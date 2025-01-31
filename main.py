import asyncio
import os
import json
import sqlite3
import logging
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import strip_markdown
from dotenv import load_dotenv

from aiohttp.web_response import json_response
from apscheduler.schedulers.asyncio import AsyncIOScheduler


import telegram
from pycparser.ply.yacc import token
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler
)
from deepseek_api import DeepSeekAPI  # Ваш модуль для работы с DeepSeek
from yandexgpt_api import YandexGptAPI
from database import Database

# Конфигурация
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YC_FOLDER_ID = os.getenv("YC_FOLDER_ID")
YC_SECRET_ID = os.getenv("YC_SECRET_ID")
DB_NAME = "reminders.db"
ALLOWED_USERS = [313049106]  # ID разрешенных пользователей
ADMIN_ID = 313049106  # ID администратора
DT_FORMAT = "%Y/%m/%d, %H:%M"
OPTIMAL_TASKS_DELTA = timedelta(hours=1)

# Валидация времени
def validate_time(time_str: str) -> bool:
    try:
        datetime.strptime(time_str, "%H:%M")
        return True
    except ValueError:
        return False

def parse_datetime(datetime_str: str) -> datetime:
    return datetime.strptime(datetime_str, DT_FORMAT)

def short_format_datetime(datetime_value: datetime) -> str:
    if datetime.now().date() == datetime_value.date():
        return datetime_value.strftime("%H:%M")
    return datetime_value.strftime(DT_FORMAT)

class ReminderBot:
    def __init__(self):
        self.db = Database()
        self.deepseek = DeepSeekAPI()
        self.yandexgpt = YandexGptAPI(YC_FOLDER_ID, YC_SECRET_ID)
        self.scheduler = AsyncIOScheduler()
        self.bot = Bot(token=BOT_TOKEN)

    def user_is_admin(self, user: dict):
        return user.get("is_admin", False) or user.get("telegram_id", 0) == ADMIN_ID

    def user_id_is_allowed(self, user_id: int):
        user = self.db.get_user(user_id)
        return user is not None and user.get("is_allowed", False) or user_id in ALLOWED_USERS

    def select_nearest_time_for_tag(self, user_id, tag_name) -> datetime:
        for tag in self.db.get_user_tags(user_id):
            if tag["name"] == tag_name:
                tasks = self.db.list_reminders_by_tag(user_id, tag["id"])
                tasks_timestamps = [parse_datetime(task["due_time"]) for task in tasks]
                tasks_timestamps.sort()
                for i in range(len(tasks_timestamps) - 1):
                    if tasks_timestamps[i] <= datetime.now():
                        continue
                    if tasks_timestamps[i+1] - tasks_timestamps[i] > OPTIMAL_TASKS_DELTA:
                        return tasks_timestamps[i] + OPTIMAL_TASKS_DELTA
        return datetime.now() + OPTIMAL_TASKS_DELTA


    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.user_id_is_allowed(user.id):
            return

        # Регистрация пользователя
        if not self.db.get_user(user.id):
            self.db.create_user({
                'telegram_id': user.id,
                'full_name': user.full_name,
                'username': user.username
            })

        keyboard = [
            [InlineKeyboardButton("Мои напоминания", callback_data='list_tasks')],
            [InlineKeyboardButton("Мои теги", callback_data='list_tags')],
            [InlineKeyboardButton("Помощь", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Добро пожаловать! Выберите действие:",
            reply_markup=reply_markup
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.user_id_is_allowed(user.id):
            return

        await self.bot.send_message(user.id,
        "/newtag <НазваниеТега> <НачалоПланирования> <КонецПланирования>")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.user_id_is_allowed(user.id):
            return

        # Получаем теги пользователя
        tags = self.db.get_user_tags(user.id)+[{"name": "default", "start_time": "00:00", "end_time": "23:59"}]
        tag_str = ", ".join([f"{t['name']} ({t['start_time']}-{t['end_time']})" for t in tags])

        # Формируем запрос к LLM
        response_format = '{"tagName": [{"text": "taskTitle", "time": "DT_FORMAT"}]}'.replace("DT_FORMAT", DT_FORMAT)
        system = f"Привет. Пользователь с тэгами [{tag_str}] хочет запланировать задачи. Сейчас {datetime.now().strftime(DT_FORMAT)}, распланируй эти задачи как можно раньше, но не раньше текущего времени и составь JSON в формате {response_format}. В ответе предоставь только JSON без пояснений"
        prompt = update.message.text

        try:
            logging.info(system)
            logging.info(prompt)
            response = await self.yandexgpt.query(system, prompt)
            logging.info(response)
            tasks = json.loads(strip_markdown.strip_markdown(response).strip("`"))
            context.user_data['pending_tasks'] = tasks

            # Формируем список задач для подтверждения
            for tag, items in tasks.items():
                for task in items:
                    # Сохранение в БД
                    self.db.create_unconfirmed_reminder(
                        user_id=user.id,
                        text=task['text'],
                        tag_id=tag,
                        due_time=task["time"]
                    )

            keyboard = []
            unconfirmed_reminders = self.db.list_unconfirmed_reminders(user.id)
            for unconfirmed_reminder in unconfirmed_reminders:
                print(unconfirmed_reminder)
                text = f"{unconfirmed_reminder["tag_id"]} - {unconfirmed_reminder['text']} ({short_format_datetime(parse_datetime(unconfirmed_reminder['due_time']))})"
                callback_data = f"confirm_task:{unconfirmed_reminder["id"]}"
                keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])

            keyboard.append([InlineKeyboardButton("Отменить оставшиеся", callback_data="confirm_task:remove")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "Выберите задачи для добавления:",
                reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"LLM error: {e}")
            print(traceback.format_exc())
            await update.message.reply_text("Ошибка при обработке запроса")

    async def confirm_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.user_id_is_allowed(user.id):
            return
        query = update.callback_query
        unconfirmed_task_id = query.data.split(":")[1]
        if unconfirmed_task_id == "remove":
            self.db.delete_unconfirmed_reminders(query.from_user.id)
            return

        task_data = self.db.get_unconfirmed_reminder(unconfirmed_task_id)
        # Сохранение в БД
        self.db.create_reminder(
            user_id=query.from_user.id,
            text=task_data['text'],
            tag_id=task_data['tag_id'],
            due_time=task_data['due_time']
        )
        self.db.delete_unconfirmed_reminder(unconfirmed_task_id)

        await self.bot.send_message(query.from_user.id, f"Задача {task_data['text']} добавлена!")

    async def check_reminders(self, context: ContextTypes.DEFAULT_TYPE):
        reminders = self.db.get_due_reminders(DT_FORMAT)
        for reminder in reminders:
            user = self.db.get_user(reminder['user_id'])
            await self.bot.send_message(
                chat_id=user['telegram_id'],
                text=f"⏰ Напоминание: {reminder['text']}"
            )
            self.db.mark_reminder_completed(reminder['id'])

    async def create_tag(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.user_id_is_allowed(user.id):
            return

        try:
            args = context.args
            if len(args) < 3:
                raise ValueError

            name = args[0]
            start_time = args[1]
            end_time = args[2]

            if not validate_time(start_time) or not validate_time(end_time):
                await self.bot.send_message(user.id, "❌ Неверный формат времени. Используйте HH:MM")
                return

            user_id = self.db.get_user(user.id)
            if self.db.create_tag(user.id, name, start_time, end_time):

                await self.bot.send_message(user.id, f"✅ Тег '{name}' успешно создан!")
            else:
                await self.bot.send_message(user.id, "❌ Ошибка при создании тега")
        except ValueError:
            await self.bot.send_message(user.id,
                "Использование: /newtag <название> <начало> <конец>\nПример: /newtag Работа 09:00 18:00")

    async def list_tags(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.user_id_is_allowed(user.id):
            return
        tags = self.db.get_user_tags(user.id)

        if not tags:
            await user.send_message("У вас пока нет тегов.")
            return

        response = "🏷 Ваши теги:\n" + "\n".join(
            [f"{tag['name']} ({tag['start_time']}-{tag['end_time']})" for tag in tags]
        )
        await user.send_message(response)

    async def list_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.user_id_is_allowed(user.id):
            return
        tasks = self.db.list_uncompleted_reminders(user.id)

        if not tasks:
            await user.send_message("У вас пока нет напоминаний.")
            return

        response = "🏷 Ваши напоминания:\n" + "\n".join(
            [f"{task['text']} ({short_format_datetime(parse_datetime(task['due_time']))}) [{task['tag_id']}])" for task in tasks]
        )
        await user.send_message(response)


if __name__ == "__main__":
    bot = ReminderBot()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("newtag", bot.create_tag))
    application.add_handler(CommandHandler("help", bot.help))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    application.add_handler(CallbackQueryHandler(bot.confirm_task, pattern="^confirm_task:"))
    application.add_handler(CallbackQueryHandler(bot.list_tags, pattern="^list_tags"))
    application.add_handler(CallbackQueryHandler(bot.list_tasks, pattern="^list_tasks"))
    application.add_handler(CallbackQueryHandler(bot.help, pattern="^help"))

    # Запускаем проверку напоминаний в фоне
    application.job_queue.run_repeating(bot.check_reminders, interval=60)

    application.run_polling()