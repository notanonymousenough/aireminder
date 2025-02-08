import asyncio
import os
import yaml
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
WEEKDAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье"
]

SHORT_WEEKDAYS = [
    "пн",
    "вт",
    "ср",
    "чт",
    "пт",
    "сб",
    "вск"
]

SHORT_MONTHS = [
    "янв",
    "фев",
    "мар",
    "апр",
    "май",
    "июнь",
    "июль",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
]

# Валидация времени
def validate_time(time_str: str) -> bool:
    try:
        datetime.strptime(time_str, "%H:%M")
        return True
    except ValueError:
        return False

def parse_datetime(datetime_str: str) -> datetime:
    return datetime.strptime(str(datetime_str), DT_FORMAT)

def parse_timestamp(timestamp_str) -> datetime:
    if type(timestamp_str) == str:
        timestamp_str = int(timestamp_str)
    return datetime.fromtimestamp(timestamp_str)

def parse_due_time(due_time) -> datetime:
    try:
        return parse_timestamp(due_time)
    except:
        return parse_datetime(due_time)

def short_format_datetime(datetime_value: datetime) -> str:
    if datetime.now().date() == datetime_value.date():
        return f"сегодня, {datetime_value.strftime("%H:%M")}"
    elif datetime.now().date() > datetime_value.date():
        return f"прошедшее, {datetime_value.strftime("%H:%M")}"
    elif datetime.now().date() + timedelta(days=1) == datetime_value.date():
        return f"завтра, {datetime_value.strftime("%H:%M")}"
    elif datetime.now().date() + timedelta(days=2) == datetime_value.date():
        return f"послезавтра, {datetime_value.strftime("%H:%M")}"
    elif datetime.now().date() + timedelta(days=6) <= datetime_value.date():
        return f"{SHORT_WEEKDAYS[datetime_value.weekday()]}., {datetime_value.strftime("%H:%M")}"
    elif datetime.now().date().year == datetime_value.date().year:
        return f"{datetime_value.day} {SHORT_MONTHS[datetime_value.month-1]}, {datetime_value.strftime("%H:%M")}"
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

    def is_tg_user_allowed(self, tg_user: telegram.User):
        user = self.db.get_user(tg_user.id)
        # Регистрация пользователя
        if user is None:
            self.db.create_user({
                'telegram_id': tg_user.id,
                'full_name': tg_user.full_name,
                'username': tg_user.username
            })

        return user is not None and user.get("is_allowed", False) or tg_user.id in ALLOWED_USERS

    def select_nearest_time_for_tag(self, user_id, tag_name) -> datetime:
        for tag in self.db.get_user_tags(user_id):
            if tag["name"] == tag_name:
                tasks = self.db.list_reminders_by_tag(user_id, tag["id"])
                tasks_timestamps = [parse_due_time(task["due_time"]) for task in tasks]
                tasks_timestamps.sort()
                for i in range(len(tasks_timestamps) - 1):
                    if tasks_timestamps[i] <= datetime.now():
                        continue
                    if tasks_timestamps[i+1] - tasks_timestamps[i] > OPTIMAL_TASKS_DELTA:
                        return tasks_timestamps[i] + OPTIMAL_TASKS_DELTA
        return datetime.now() + OPTIMAL_TASKS_DELTA


    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            return

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
        if not self.is_tg_user_allowed(user):
            return

        if self.user_is_admin(self.db.get_user(user.id)):
            await self.bot.send_message(user.id,
                                        "VIP команды: /allow, /ban /list")

        await self.bot.send_message(user.id,
        "/newtag <НазваниеТега> <НачалоПланирования> <КонецПланирования>")

    async def ask_llm_extract(self, tags: List[Dict], query: str) -> Dict:
        tag_str = ", ".join([f"{t['name']}" for t in tags])
        response_format = '{"tagName": [{"text": "taskTitle"}]}'
        system = f"Ты - умный ассистент пользователя, помогающий ему распланировать напоминания. Извлеки список задач из сообщения пользователя и распредели их по тегам. Учти пожелания пользователя по их количеству и привязанности к тегам. Список тегов: [{tag_str}]. В ответе предоставь только валидный JSON в формате {response_format} без пояснений. Если пользователь хочет несколько напоминаний, продублируй их в возвращаемом списке столько раз, сколько он просит, но не больше тридцати."

        logging.info(system)
        logging.info(query)
        response = await self.yandexgpt.query(system, query)
        logging.info(response)

        return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))

    async def ask_llm_plan(self, tags: List[Dict], tasks: Dict, query: str) -> Dict:
        tag_str = ", ".join([f"{t['name']} ({t['start_time']}-{t['end_time']})" for t in tags])
        response_format = '{"tagName": [{"text": "taskTitle", "time": "DT_FORMAT"}]}'.replace("DT_FORMAT", DT_FORMAT)
        current_weekday = WEEKDAYS[datetime.today().weekday()]
        system = f"Ты - умный ассистент пользователя, помогающий ему распланировать напоминания. Проставь всем извлечённым задачам из сообщения пользователя время как можно ближе к настоящему, но не раньше текущего времени {datetime.now().strftime(DT_FORMAT)} ({current_weekday}). По умолчанию считай, что напомнить нужно сегодня, если это позволяет окно планирования тега и не сказано обратное в сообщении пользователя. Учитывай пожелания пользователя, держи адекватное количество времени между задачами, а также предпочитай планировать днём, а не ночью (если это не попросил пользователь). Список тегов и окон планирования каждого из них: [{tag_str}]. На вход дается JSON с двумя полями: extracted_tasks - извлеченные задачи с разбивкой по тегам, которым нужно выставить время; user_query - запрос пользователя, пожелания из которого нужно учесть. В ответе предоставь только валидный JSON в формате {response_format} без пояснений, datetime строго в формате {DT_FORMAT}"

        logging.info(system)
        tasks_with_query = {"extracted_tasks": tasks.copy(), "user_query": query}
        logging.info(tasks_with_query)
        response = await self.yandexgpt.query(system, str(tasks_with_query))
        logging.info(response)

        return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            return

        # Получаем теги пользователя
        tags = self.db.get_user_tags(user.id)+[{"name": "default", "start_time": "00:00", "end_time": "23:59"}]

        try:
            query = update.message.text.replace("\n", ";")
            tasks_without_time = await self.ask_llm_extract(tags, query)
            tasks = await self.ask_llm_plan(tags, tasks_without_time, query)
            context.user_data['pending_tasks'] = tasks

            # Формируем список задач для подтверждения
            for tag, items in tasks.items():
                for task in items:
                    # Сохранение в БД
                    self.db.create_unconfirmed_reminder(
                        user_id=user.id,
                        text=task['text'],
                        tag_id=tag,
                        due_time=parse_datetime(task["time"])
                    )

            keyboard = []
            unconfirmed_reminders = self.db.list_unconfirmed_reminders(user.id)
            for unconfirmed_reminder in unconfirmed_reminders:
                text = f"{short_format_datetime(parse_due_time(unconfirmed_reminder['due_time']))} {unconfirmed_reminder['text']} [{unconfirmed_reminder["tag_id"]}]"
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
        if not self.is_tg_user_allowed(user):
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
            due_time=parse_timestamp(task_data['due_time'])
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
        if not self.is_tg_user_allowed(user):
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

    async def allow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            return
        if not self.user_is_admin(self.db.get_user(user.id)):
            return

        try:
            args = context.args
            if len(args) != 1:
                raise ValueError

            telegram_id = args[0]
            if self.db.get_user(telegram_id) and self.db.update_user_permission(telegram_id, True):
                await self.bot.send_message(user.id, f"✅ Доступ пользователю '{telegram_id}' успешно предоставлен!")
        except ValueError:
            await self.bot.send_message(user.id,
                "Использование: /allow <telegram_id>")

    async def user_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            return
        if not self.user_is_admin(self.db.get_user(user.id)):
            return

        keyboard = []
        users = self.db.list_users()
        for user in users:
            text = f"{user["full_name"]} - tg Id: {user['telegram_id']} ({"Allowed" if user["is_allowed"] else "New"}) {"VIP" if user["is_admin"] else ""}"
            callback_data = f"user_get:{user["telegram_id"]}"
            keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Выберите пользователя для подробностей:",
            reply_markup=reply_markup
        )

    async def user_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            return
        if not self.user_is_admin(self.db.get_user(user.id)):
            return

        query = update.callback_query
        telegram_id = query.data.split(":")[1]
        db_user = self.db.get_user(telegram_id)
        await self.bot.send_message(user.id, str(db_user))

    async def disallow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            return
        if not self.user_is_admin(self.db.get_user(user.id)):
            return

        try:
            args = context.args
            if len(args) != 1:
                raise ValueError

            telegram_id = args[0]
            if self.db.get_user(telegram_id) and self.db.update_user_permission(telegram_id, False):
                await self.bot.send_message(user.id, f"✅ Доступ пользователю '{telegram_id}' успешно оторван!")
        except ValueError:
            await self.bot.send_message(user.id,
                "Использование: /ban <telegram_id>")

    async def list_tags(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
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
        if not self.is_tg_user_allowed(user):
            return
        tasks = self.db.list_uncompleted_reminders(user.id)

        if not tasks:
            await user.send_message("У вас пока нет напоминаний.")
            return

        response = "🏷 Ваши напоминания:\n" + "\n".join(
            [f"{task['text']} ({short_format_datetime(parse_due_time(task['due_time']))}) [{task['tag_id']}])" for task in tasks]
        )
        await user.send_message(response)


if __name__ == "__main__":
    bot = ReminderBot()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("newtag", bot.create_tag))
    application.add_handler(CommandHandler("help", bot.help))
    application.add_handler(CommandHandler("allow", bot.allow))
    application.add_handler(CommandHandler("ban", bot.disallow))
    application.add_handler(CommandHandler("list", bot.user_list))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    application.add_handler(CallbackQueryHandler(bot.confirm_task, pattern="^confirm_task:"))
    application.add_handler(CallbackQueryHandler(bot.list_tags, pattern="^list_tags"))
    application.add_handler(CallbackQueryHandler(bot.list_tasks, pattern="^list_tasks"))
    application.add_handler(CallbackQueryHandler(bot.user_get, pattern="^user_get"))
    application.add_handler(CallbackQueryHandler(bot.help, pattern="^help"))

    # Запускаем проверку напоминаний в фоне
    application.job_queue.run_repeating(bot.check_reminders, interval=60)

    application.run_polling()