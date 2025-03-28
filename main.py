import asyncio
import os
import yaml
import logging
import traceback
from datetime import datetime, timedelta, timezone, time
from typing import Dict, List, Optional, Any, Union, Tuple
import strip_markdown
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cachetools import TTLCache

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, BotCommand, BotCommandScopeDefault, \
    BotCommandScopeChat
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler
)
from deepseek_api import DeepSeekAPI
from yandexgpt_api import YandexGptAPI
from database import Database
from config import *
from utils import *

# Настройка логирования
logging.basicConfig(
    filename="main.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Создаем отдельный обработчик для вывода логов в консоль
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(console_formatter)

logger = logging.getLogger(__name__)
logger.addHandler(console_handler)

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YC_FOLDER_ID = os.getenv("YC_FOLDER_ID")
YC_SECRET_ID = os.getenv("YC_SECRET_ID")

if not BOT_TOKEN or not YC_FOLDER_ID or not YC_SECRET_ID:
    logger.error("Не указаны обязательные переменные окружения")
    raise EnvironmentError("Отсутствуют обязательные переменные окружения")

class ReminderBot:
    """Бот для управления напоминаниями с использованием LLM."""

    def __init__(self) -> None:
        """Инициализация бота и его зависимостей."""
        self.db = Database()
        self.deepseek = DeepSeekAPI()
        self.yandexgpt = YandexGptAPI(YC_FOLDER_ID, YC_SECRET_ID)
        self.scheduler = AsyncIOScheduler()
        self.db_tasks_listing_page = 0
        self.bot = Bot(token=BOT_TOKEN)
        # Добавляем кэш для часто запрашиваемых данных (TTL = 5 минут)
        self.user_cache = TTLCache(maxsize=100, ttl=300)
        self.last_log_position = 0  # Для оптимизации чтения лога
        logger.info("ReminderBot initialized")

    def user_is_admin(self, user: Dict[str, Any]) -> bool:
        """Проверяет, является ли пользователь администратором.

        Args:
            user: Словарь с данными пользователя

        Returns:
            True, если пользователь администратор, иначе False
        """
        return user.get("is_admin", False) or user.get("telegram_id", 0) == ADMIN_ID

    def is_tg_user_allowed(self, tg_user: telegram.User) -> bool:
        """Проверяет, разрешено ли пользователю использовать бота.

        Args:
            tg_user: Объект пользователя Telegram

        Returns:
            True, если пользователю разрешен доступ, иначе False
        """
        # Сначала проверяем кэш
        cache_key = f"user_{tg_user.id}"
        if cache_key in self.user_cache:
            return self.user_cache[cache_key]

        user = self.db.get_user(tg_user.id)

        # Регистрация пользователя, если он новый
        if user is None:
            logger.info(f"Регистрация нового пользователя: {tg_user.id} ({tg_user.full_name})")
            self.db.create_user({
                'telegram_id': tg_user.id,
                'full_name': tg_user.full_name,
                'username': tg_user.username
            })
            user = self.db.get_user(tg_user.id)

        is_allowed = user is not None and (user.get("is_allowed", False) or tg_user.id in ALLOWED_USERS)
        # Сохраняем результат в кэше
        self.user_cache[cache_key] = is_allowed

        return is_allowed

    def select_nearest_time_for_tag(self, user_id: int, tag_name: str) -> datetime:
        """Вычисляет оптимальное время для нового напоминания.

        Args:
            user_id: ID пользователя
            tag_name: Название тега

        Returns:
            Рекомендуемое время для нового напоминания
        """
        for tag in self.db.get_user_tags(user_id):
            if tag["name"] == tag_name:
                tasks = self.db.list_reminders_by_tag(user_id, tag["id"])
                tasks_timestamps = [parse_timestamp(task["due_time"]) for task in tasks]
                tasks_timestamps.sort()

                for i in range(len(tasks_timestamps) - 1):
                    if tasks_timestamps[i] <= datetime.now(SERVER_TIMEZONE):
                        continue
                    if tasks_timestamps[i + 1] - tasks_timestamps[i] > OPTIMAL_TASKS_DELTA:
                        return tasks_timestamps[i] + OPTIMAL_TASKS_DELTA

        return datetime.now(SERVER_TIMEZONE) + OPTIMAL_TASKS_DELTA

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /start.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка доступа от неразрешенного пользователя: {user.id}")
            await update.message.reply_text(
                "Извините, у вас нет доступа к этому боту. Обратитесь к администратору."
            )
            return

        keyboard = [
            [InlineKeyboardButton("Мои напоминания", callback_data='list_tasks')],
            [InlineKeyboardButton("Мои теги", callback_data='list_tags')],
            [InlineKeyboardButton("Помощь", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await update.message.reply_text(
                "Добро пожаловать! Выберите действие:",
                reply_markup=reply_markup
            )
            logger.info(f"Пользователь {user.id} запустил бота")
        except Exception as e:
            logger.error(f"Ошибка при отправке приветствия: {e}")

    async def ask_llm_extract(self, tags: List[Dict], query: str) -> Dict:
        """Обращается к LLM для извлечения задач из запроса пользователя.

        Args:
            tags: Список тегов пользователя
            query: Текст запроса пользователя

        Returns:
            Словарь с извлеченными задачами

        Raises:
            Exception: При ошибке взаимодействия с LLM или обработке ответа
        """
        tag_str = ", ".join([f"{t['name']}" for t in tags])
        response_format = '{"tagName": [{"text": "taskTitle"}]}'
        system = (
            f"Ты - умный ассистент пользователя, помогающий ему распланировать напоминания. "
            f"Извлеки список задач из сообщения пользователя и распредели их по тегам. "
            f"Учти пожелания пользователя по их количеству и привязанности к тегам. "
            f"Список тегов: [{tag_str}]. "
            f"В ответе предоставь только валидный JSON в формате {response_format} без пояснений. "
            f"Если пользователь хочет несколько напоминаний, продублируй их в возвращаемом списке "
            f"столько раз, сколько он просит, но не больше тридцати."
        )

        logger.info(f"LLM extract query: {query}")
        try:
            # Добавляем повторную попытку для повышения надежности
            for attempt in range(3):
                try:
                    response = await self.yandexgpt.query(system, query)
                    logger.info(f"LLM extract response received")
                    return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))
                except Exception as e:
                    if attempt < 2:
                        logger.warning(
                            f"Попытка {attempt + 1} извлечения задач из LLM не удалась: {e}. Повторная попытка...")
                        await asyncio.sleep(1)
                    else:
                        raise
        except Exception as e:
            logger.error(f"Ошибка при извлечении задач из LLM: {e}")
            raise

    async def ask_llm_plan(self, tags: List[Dict], tasks: Union[Dict, str, Any], query: str) -> Dict:
        """Обращается к LLM для планирования времени извлеченных задач.

        Args:
            tags: Список тегов пользователя
            tasks: Словарь извлеченных задач или другой объект
            query: Исходный запрос пользователя

        Returns:
            Словарь с распланированными задачами

        Raises:
            Exception: При ошибке взаимодействия с LLM или обработке ответа
        """
        tag_str = ", ".join([f"{t['name']} ({t['start_time']}-{t['end_time']})" for t in tags])
        response_format = '{"tagName": [{"text": "taskTitle", "time": "DT_FORMAT"}]}'.replace("DT_FORMAT", DT_FORMAT)
        current_weekday = WEEKDAYS[datetime.today().weekday()]

        system = (
            f"Ты - умный ассистент пользователя, помогающий ему распланировать напоминания. "
            f"Проставь всем извлечённым задачам из сообщения пользователя время как можно ближе "
            f"к настоящему, но не раньше текущего времени {datetime.now(SERVER_TIMEZONE).strftime(DT_FORMAT)} "
            f"({current_weekday}). По умолчанию считай, что напомнить нужно сегодня, если это позволяет "
            f"окно планирования тега и не сказано обратное в сообщении пользователя. "
            f"Учитывай пожелания пользователя, держи адекватное количество времени между задачами, "
            f"а также предпочитай планировать днём, а не ночью (если это не попросил пользователь). "
            f"Список тегов и окон планирования каждого из них: [{tag_str}]. "
            f"На вход дается JSON с двумя полями: extracted_tasks - извлеченные задачи с разбивкой "
            f"по тегам, которым нужно выставить время; user_query - запрос пользователя, пожелания "
            f"из которого нужно учесть. В ответе предоставь только валидный JSON в формате {response_format} "
            f"без пояснений, datetime строго в формате {DT_FORMAT}"
        )

        logger.info(f"LLM plan for tasks")

        # Безопасное создание словаря запроса в зависимости от типа tasks
        if isinstance(tasks, dict):
            extracted_tasks = tasks.copy()
        else:
            # Если tasks не словарь, преобразуем его в строку
            extracted_tasks = str(tasks)

        tasks_with_query = {"extracted_tasks": extracted_tasks, "user_query": query}

        try:
            # Добавляем повторную попытку для повышения надежности
            for attempt in range(3):
                try:
                    response = await self.yandexgpt.query(system, str(tasks_with_query))
                    logger.info(f"LLM plan response received")
                    return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))
                except Exception as e:
                    if attempt < 2:
                        logger.warning(
                            f"Попытка {attempt + 1} планирования задач через LLM не удалась: {e}. Повторная попытка...")
                        await asyncio.sleep(1)
                    else:
                        raise
        except Exception as e:
            logger.error(f"Ошибка при планировании задач через LLM: {e}")
            raise

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает текстовые сообщения пользователя.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Сообщение от неразрешенного пользователя: {user.id}")
            return

        # Проверяем, не слишком ли короткое сообщение
        query = update.message.text.replace("\n", ";")
        if len(query.strip()) < 3:
            await update.message.reply_text(
                "Пожалуйста, введите более подробное описание задачи или напоминания."
            )
            return

        # Отправляем индикатор печати
        await self.bot.send_chat_action(chat_id=user.id, action=telegram.constants.ChatAction.TYPING)

        # Получаем теги пользователя
        tags = self.db.get_user_tags(user.id) + [{"name": "default", "start_time": "00:00", "end_time": "23:59"}]

        try:
            # Извлекаем задачи из сообщения
            tasks_without_time = await self.ask_llm_extract(tags, query)

            # Планируем время для задач
            tasks = await self.ask_llm_plan(tags, tasks_without_time, query)
            context.user_data['pending_tasks'] = tasks

            # Сохраняем неподтвержденные напоминания
            created_count = 0
            for tag, items in tasks.items():
                for task in items:
                    # Сохранение в БД
                    due_time = parse_datetime(task["time"])
                    if due_time:
                        self.db.create_unconfirmed_reminder(
                            user_id=user.id,
                            text=task['text'],
                            tag_id=tag,
                            due_time=due_time
                        )
                        created_count += 1
                    else:
                        logger.warning(f"Не удалось распарсить время '{task['time']}' для задачи '{task['text']}'")

            # Формируем клавиатуру для подтверждения задач
            keyboard = []
            unconfirmed_reminders = self.db.list_unconfirmed_reminders(user.id)

            # Группируем задачи по дате для более удобного отображения
            grouped_tasks = self._group_unconfirmed_tasks_by_date(unconfirmed_reminders)

            for date_group, tasks in grouped_tasks.items():
                keyboard.append([InlineKeyboardButton(f"📅 {date_group}", callback_data="ignore")])  # Use ignore to bypass

                for task in tasks:
                    due_time = parse_timestamp(task['due_time'])
                    time_str = due_time.strftime('%H:%M')
                    text = f"{time_str} - {task['text']} [{task['tag_id']}]"
                    callback_data = f"confirm_task:{task['id']}"
                    keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])

            keyboard.append([InlineKeyboardButton("Отменить оставшиеся", callback_data="confirm_task:remove")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            if created_count > 0:
                await update.message.reply_text(
                    "Выберите задачи для добавления:",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    "Не удалось создать напоминания из вашего сообщения. Попробуйте перефразировать.")

            logger.info(f"Создано {created_count} неподтвержденных напоминаний для пользователя {user.id}")

        except Exception as e:
            logger.error(f"Ошибка при обработке сообщения: {e}\n{traceback.format_exc()}")
            await update.message.reply_text(
                "Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте еще раз.")

    def _group_unconfirmed_tasks_by_date(self, tasks: List[Dict]) -> Dict[str, List[Dict]]:
        """Группирует неподтвержденные задачи по дате.

        Args:
            tasks: Список неподтвержденных задач

        Returns:
            Словарь с задачами, сгруппированными по датам
        """
        grouped = {}

        for task in tasks:
            due_time = parse_timestamp(task['due_time'])
            today = datetime.now(SERVER_TIMEZONE).date()
            tomorrow = today + timedelta(days=1)

            if due_time.date() == today:
                date_group = "Сегодня"
            elif due_time.date() == tomorrow:
                date_group = "Завтра"
            else:
                date_group = format_date(due_time)

            if date_group not in grouped:
                grouped[date_group] = []

            grouped[date_group].append(task)

        # Сортируем задачи внутри каждой группы по времени
        for date_group in grouped:
            grouped[date_group].sort(key=lambda x: parse_timestamp(x['due_time']))

        # Возвращаем словарь с отсортированными ключами
        return {k: grouped[k] for k in sorted(grouped.keys())}

    async def confirm_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает подтверждение задачи пользователем."""
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка подтверждения задачи от неразрешенного пользователя: {user.id}")
            return

        query = update.callback_query

        # Пропускаем обработку заголовков дат
        if query.data == "date_header":
            await self.bot.answer_callback_query(query.id)
            return

        unconfirmed_task_id = query.data.split(":")[1]

        try:
            if unconfirmed_task_id == "remove":
                deleted_count = self.db.delete_unconfirmed_reminders(query.from_user.id)
                logger.info(f"Удалено {deleted_count} неподтвержденных напоминаний для пользователя {user.id}")
                await self.bot.answer_callback_query(query.id, text=f"Отменено {deleted_count} напоминаний")

                # Обновляем сообщение, чтобы удалить кнопки
                if query.message:
                    await query.message.edit_text(f"Отменено {deleted_count} напоминаний")
                return

            task_data = self.db.get_unconfirmed_reminder(unconfirmed_task_id)
            if not task_data:
                logger.warning(f"Попытка подтвердить несуществующее напоминание: {unconfirmed_task_id}")
                await self.bot.answer_callback_query(query.id, text="Напоминание не найдено")
                return

            # Сохранение в БД
            reminder_id = self.db.create_reminder(
                user_id=query.from_user.id,
                text=task_data['text'],
                tag_id=task_data['tag_id'],
                due_time=parse_timestamp(task_data['due_time'])
            )

            self.db.delete_unconfirmed_reminder(unconfirmed_task_id)

            if reminder_id:
                # Обновление клавиатуры в интерфейсе - создаем новую клавиатуру
                if query.message and query.message.reply_markup:
                    new_keyboard = []
                    for row in query.message.reply_markup.inline_keyboard:
                        new_row = []
                        for button in row:
                            if button.callback_data == query.data:
                                # Создаем новую кнопку с измененным текстом
                                new_row.append(InlineKeyboardButton(
                                    f"✅ {button.text}",
                                    callback_data=button.callback_data
                                ))
                            else:
                                new_row.append(button)
                        new_keyboard.append(new_row)

                    try:
                        await query.message.edit_reply_markup(InlineKeyboardMarkup(new_keyboard))
                    except Exception as e:
                        logger.error(f"Не удалось обновить клавиатуру: {e}")

                await self.bot.send_message(
                    query.from_user.id,
                    f"✅ Задача «{task_data['text']}» добавлена на {short_format_datetime(parse_timestamp(task_data['due_time']))}!"
                )
                logger.info(f"Пользователь {user.id} подтвердил задачу '{task_data['text']}'")
            else:
                await self.bot.send_message(query.from_user.id, "❌ Не удалось сохранить задачу")

            await self.bot.answer_callback_query(query.id)

        except Exception as e:
            logger.error(f"Ошибка при подтверждении задачи: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def ignore(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает нулевой колбэк."""
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка нулевого колбэка от неразрешенного пользователя: {user.id}")
            return

        query = update.callback_query

        try:
            await self.bot.answer_callback_query(query.id)
            return
        except Exception as e:
            logger.error(f"Ошибка при подтверждении задачи: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def reschedule_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на перенос напоминания.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка переноса задачи от неразрешенного пользователя: {user.id}")
            return

        query = update.callback_query
        parts = query.data.split(":")

        if len(parts) < 3:
            logger.error(f"Некорректный формат callback_data: {query.data}")
            await self.bot.answer_callback_query(query.id, text="Некорректный формат запроса")
            return

        task_id = parts[1]
        reschedule_delta = parts[2]

        try:
            # Определяем величину переноса
            delta = self._get_reschedule_delta(reschedule_delta)
            task_data = self.db.get_reminder(task_id)

            if not task_data:
                logger.warning(f"Попытка перенести несуществующее напоминание: {task_id}")
                await self.bot.answer_callback_query(query.id, text="Напоминание не найдено")
                return

            new_due_dt = datetime.now(SERVER_TIMEZONE) + delta

            if self.db.reschedule(task_id, new_due_dt):
                # Обновляем сообщение, добавляя информацию о переносе
                if query.message:
                    try:
                        new_text = f"{query.message.text}\n\n⏰ Перенесено на {short_format_datetime(new_due_dt)}"
                        await query.message.edit_text(new_text)
                    except Exception as e:
                        logger.error(f"Не удалось обновить сообщение: {e}")

                await self.bot.send_message(
                    query.from_user.id,
                    f"⏰ Задача «{task_data['text']}» перенесена на {short_format_datetime(new_due_dt)}!"
                )
                logger.info(f"Пользователь {user.id} перенес задачу '{task_data['text']}' на {new_due_dt}")
            else:
                await self.bot.send_message(query.from_user.id, "❌ Не удалось перенести задачу")

            await self.bot.answer_callback_query(query.id)

        except Exception as e:
            logger.error(f"Ошибка при переносе задачи: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    def _get_reschedule_delta(self, reschedule_type: str) -> timedelta:
        """Определяет интервал для переноса напоминания.

        Args:
            reschedule_type: Тип переноса

        Returns:
            Временной интервал для переноса
        """
        deltas = {
            "hour": timedelta(hours=1),
            "8hours": timedelta(hours=8),
            "day": timedelta(days=1),
            "2days": timedelta(days=2),
            "week": timedelta(weeks=1),
            "month": timedelta(days=31),
            "3months": timedelta(days=93)
        }

        if reschedule_type in deltas:
            return deltas[reschedule_type]

        if reschedule_type == "evening":
            delta = timedelta(minutes=30)
            while (datetime.now(SERVER_TIMEZONE) + delta).hour <= 19:
                delta += timedelta(hours=1)
            return delta

        if reschedule_type == "weekends":
            delta = timedelta(days=1)
            while (datetime.now(SERVER_TIMEZONE) + delta).weekday() <= 4:
                delta += timedelta(days=1)
            return delta

        # Значение по умолчанию
        return timedelta(minutes=30)

    async def check_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Проверяет и отправляет напоминания, для которых наступило время.

        Args:
            context: Контекст планировщика
        """
        dt = datetime.now(SERVER_TIMEZONE)
        try:
            reminders = self.db.get_due_reminders(dt)
            logger.info(f"Проверка напоминаний: найдено {len(reminders)} активных напоминаний")

            # Группируем напоминания по пользователям для оптимизации
            user_reminders = {}
            for reminder in reminders:
                user_id = reminder['user_id']
                if user_id not in user_reminders:
                    user_reminders[user_id] = []
                user_reminders[user_id].append(reminder)

            for user_id, user_reminder_list in user_reminders.items():
                try:
                    user = self.db.get_user(user_id)
                    if not user:
                        logger.error(f"Пользователь {user_id} не найден")
                        continue

                    for reminder in user_reminder_list:
                        reminder_time = parse_timestamp(reminder["due_time"])

                        # Проверка, не отправляем напоминание раньше времени
                        if reminder_time > dt:
                            err_message = f"Попытка отправить напоминание {reminder['id']} раньше времени: {reminder_time} > {dt}"
                            logger.error(err_message)
                            await self.bot.send_message(chat_id=ADMIN_ID, text=err_message)
                            continue

                        try:
                            # Подготовка сообщения с рекомендациями
                            assist = ""
                            if reminder.get("assist") and reminder["assist"].strip():
                                assist = f"\n\n---\n{reminder['assist']}"

                            # Создание клавиатуры для отложенных напоминаний
                            keyboard = self._create_reschedule_keyboard(reminder["id"])
                            reply_markup = InlineKeyboardMarkup(keyboard)

                            # Отправка напоминания
                            await self.bot.send_message(
                                chat_id=user['telegram_id'],
                                text=f"⏰ Напоминание: {reminder['text']}{assist}",
                                reply_markup=reply_markup
                            )

                            # Отмечаем напоминание как отправленное
                            self.db.mark_reminder_completed(reminder['id'])
                            logger.info(f"Отправлено напоминание {reminder['id']} пользователю {user['telegram_id']}")

                        except Exception as e:
                            logger.error(f"Ошибка при отправке напоминания {reminder['id']}: {e}")

                except Exception as e:
                    logger.error(f"Ошибка при обработке напоминаний пользователя {user_id}: {e}")

        except Exception as e:
            logger.error(f"Ошибка при проверке напоминаний: {e}")

    def _create_reschedule_keyboard(self, reminder_id: int) -> List[List[InlineKeyboardButton]]:
        """Создает клавиатуру для переноса напоминания.

        Args:
            reminder_id: ID напоминания

        Returns:
            Кнопки для клавиатуры
        """
        return [
            [
                InlineKeyboardButton("через час", callback_data=f"reschedule_task:{reminder_id}:hour"),
                InlineKeyboardButton("через день", callback_data=f"reschedule_task:{reminder_id}:day"),
                InlineKeyboardButton("через неделю", callback_data=f"reschedule_task:{reminder_id}:week"),
            ],
            [
                InlineKeyboardButton("через 8 часов", callback_data=f"reschedule_task:{reminder_id}:8hours"),
                InlineKeyboardButton("через 2 дня", callback_data=f"reschedule_task:{reminder_id}:2days"),
                InlineKeyboardButton("через месяц", callback_data=f"reschedule_task:{reminder_id}:month"),
            ],
            [
                InlineKeyboardButton("через 3 месяца", callback_data=f"reschedule_task:{reminder_id}:3months"),
                InlineKeyboardButton("вечером", callback_data=f"reschedule_task:{reminder_id}:evening"),
                InlineKeyboardButton("в выходные", callback_data=f"reschedule_task:{reminder_id}:weekends"),
            ],
            [
                InlineKeyboardButton("✓ Выполнено", callback_data=f"complete_task:{reminder_id}")
            ]
        ]

    async def complete_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает отметку задачи как выполненной.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка отметки задачи от неразрешенного пользователя: {user.id}")
            return

        query = update.callback_query
        task_id = query.data.split(":")[1]

        try:
            task_data = self.db.get_reminder(task_id)
            if not task_data:
                logger.warning(f"Попытка отметить несуществующее напоминание: {task_id}")
                await self.bot.answer_callback_query(query.id, text="Напоминание не найдено")
                return

            if self.db.mark_reminder_completed(task_id):
                # Обновляем сообщение
                if query.message:
                    try:
                        new_text = f"{query.message.text}\n\n✅ Выполнено!"
                        await query.message.edit_text(new_text)
                    except Exception as e:
                        logger.error(f"Не удалось обновить сообщение: {e}")

                await self.bot.answer_callback_query(query.id, text="Задача отмечена как выполненная")
                logger.info(f"Пользователь {user.id} отметил задачу '{task_data['text']}' как выполненную")
            else:
                await self.bot.answer_callback_query(query.id, text="Не удалось обновить статус задачи")

        except Exception as e:
            logger.error(f"Ошибка при отметке задачи как выполненной: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def ask_llm_assist(self, query: str) -> Dict:
        """Запрашивает у LLM дополнительные рекомендации для задачи.

        Args:
            query: Текст задачи

        Returns:
            Словарь с рекомендациями

        Raises:
            Exception: При ошибке взаимодействия с LLM или обработке ответа
        """
        response_format = '{"hasAssist": bool, "assist": "text"}'
        system = (
            f"Ты - умный ассистент пользователя, помогающий ему выполнять свои задачи. "
            f"Подумай, какая информация может помочь пользователю выполнить задачу и составь "
            f"небольшой текст размером в один параграф с конкретными пунктами-советами и "
            f"небольшим вступлением, чтобы пользователь не испугался, а понял, что ты помогаешь. "
            f"Разделяй советы новой строкой. Учитывай, что пользователь и сам бы справился с задачей, "
            f"он умный и знает что делать, но действительно полезный совет не помешал бы ему. "
            f"Будь вежливым и дружелюбным. Если задача слишком простая и супер интересных советов нет - "
            f"вместо этого просто подбодри его, но не объясняй очевидные вещи. "
            f"На вход дается текст задачи пользователя. В ответе предоставь только валидный JSON "
            f"в формате {response_format} без пояснений"
        )

        logger.info(f"Запрос советов LLM для задачи: {query}")
        try:
            for attempt in range(3):
                try:
                    response = await self.yandexgpt.query(system, str(query))
                    logger.info(f"Получен ответ от LLM с советами")
                    return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"Попытка {attempt + 1} получения советов не удалась: {e}. Повторная попытка...")
                        await asyncio.sleep(1)
                    else:
                        raise
        except Exception as e:
            logger.error(f"Ошибка при получении советов LLM: {e}")
            return {"hasAssist": False, "assist": ""}

    async def assist(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Создает рекомендации для предстоящих напоминаний.

        Args:
            context: Контекст планировщика
        """
        dt = datetime.now(SERVER_TIMEZONE) + timedelta(hours=5)
        try:
            reminders = self.db.get_due_reminders(dt)
            processed = 0

            for reminder in reminders:
                # Пропускаем напоминания, для которых уже есть рекомендации
                if reminder.get("assist") is not None and reminder["assist"].strip():
                    continue

                try:
                    # Получаем рекомендации от LLM
                    assist = await self.ask_llm_assist(reminder["text"])
                    if not isinstance(assist, dict) or not assist.get("hasAssist", False) or not assist.get("assist"):
                        continue

                    # Сохраняем рекомендации в БД
                    self.db.update_task_assist(reminder["id"], assist["assist"])
                    processed += 1

                except Exception as e:
                    logger.error(f"Ошибка при создании рекомендаций для напоминания {reminder['id']}: {e}")

            if processed > 0:
                logger.info(f"Создано рекомендаций для {processed} напоминаний")

        except Exception as e:
            logger.error(f"Ошибка в планировщике рекомендаций: {e}")

    async def daily(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Отправляет ежедневное расписание задач.

        Args:
            context: Контекст планировщика
        """
        dt = datetime.now(SERVER_TIMEZONE).replace(hour=23, minute=59, second=59)
        logger.info("Запуск ежедневного уведомления")

        try:
            reminders = self.db.get_due_reminders(dt)
            reminders_per_user = {}

            # Группируем напоминания по пользователям
            for reminder in reminders:
                user_id = reminder["user_id"]
                if user_id not in reminders_per_user:
                    reminders_per_user[user_id] = []
                reminders_per_user[user_id].append(reminder)

            # Отправляем сообщения каждому пользователю
            for user_id, user_reminders in reminders_per_user.items():
                if not user_reminders:
                    continue

                user = self.db.get_user(user_id)
                if not user:
                    logger.warning(f"Пользователь {user_id} не найден для ежедневного уведомления")
                    continue

                # Группируем задачи по тегам для более наглядного отображения
                tasks_by_tag = {}
                for reminder in user_reminders:
                    tag_id = reminder['tag_id']
                    if tag_id not in tasks_by_tag:
                        tasks_by_tag[tag_id] = []
                    tasks_by_tag[tag_id].append(reminder)

                # Формируем список задач на день
                tasks_text = ["Доброе утро! На сегодня запланировано:"]

                for tag_id, tag_tasks in tasks_by_tag.items():
                    tasks_text.append(f"\n🏷 {tag_id}:")
                    for task in tag_tasks:
                        due_time = parse_timestamp(task['due_time'])
                        time_str = due_time.strftime('%H:%M')
                        tasks_text.append(f"• {time_str} - {task['text']}")

                if len(tasks_text) > 1:  # Проверяем, что есть хотя бы один тег с задачами
                    message = "\n".join(tasks_text)
                    await self.bot.send_message(chat_id=user['telegram_id'], text=message)
                    logger.info(f"Отправлено ежедневное уведомление пользователю {user_id}")

        except Exception as e:
            logger.error(f"Ошибка при отправке ежедневного уведомления: {e}")

    async def monitor(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Выполняет мониторинг состояния бота.

        Args:
            context: Контекст планировщика
        """
        dt = datetime.now(SERVER_TIMEZONE)
        logger.info("Запуск мониторинга")

        try:
            # Проверяем задержки в отправке напоминаний
            reminders = self.db.get_due_reminders(dt)
            for reminder in reminders:
                reminder_time = parse_timestamp(reminder["due_time"])
                if dt - reminder_time > timedelta(minutes=5):
                    err_message = f"Напоминание {reminder['id']} не отправлено более 5 минут! Время: {reminder_time}, сейчас: {dt}"
                    logger.error(err_message)
                    await self.bot.send_message(chat_id=ADMIN_ID, text=err_message)

            # Анализируем логи на наличие ошибок
            await self._check_logs_for_errors()

        except Exception as e:
            logger.error(f"Ошибка в мониторинге: {e}")
            await self.bot.send_message(chat_id=ADMIN_ID, text=f"Ошибка в мониторинге: {e}")

    async def _check_logs_for_errors(self) -> None:
        """Проверяет логи на наличие ошибок."""
        try:
            with open("main.log", "r") as f:
                # Определяем, где остановились в прошлый раз
                if self.last_log_position > 0:
                    f.seek(self.last_log_position)

                found_errors = set()
                lines = f.readlines()

                # Запоминаем позицию для следующего запуска
                self.last_log_position = f.tell()

                for line in lines:
                    lline = line.lower()
                    if "error" in lline or "exception" in lline or "fail" in lline:
                        if len(found_errors) > 10:
                            found_errors.add("... и другие ошибки (превышен лимит вывода)")
                            break
                        found_errors.add(line[:1000])  # Обрезаем длинные строки

                if found_errors:
                    err_message = f"Обнаружены новые ошибки в логах:\n\n{'\n'.join(found_errors)}"
                    await self.bot.send_message(chat_id=ADMIN_ID, text=err_message)
        except Exception as e:
            logger.error(f"Ошибка при проверке логов: {e}")

    async def call_monitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду запуска мониторинга.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка запуска мониторинга от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка запуска мониторинга от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            await self.monitor(context)
            await update.message.reply_text("Мониторинг выполнен успешно")
            logger.info(f"Пользователь {user.id} запустил мониторинг вручную")
        except Exception as e:
            logger.error(f"Ошибка при ручном запуске мониторинга: {e}")
            await update.message.reply_text(f"Ошибка при выполнении мониторинга: {e}")

    async def call_clear_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду очистки лога.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка очистки лога от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка очистки лога от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            # Сначала отправляем текущий лог
            await update.message.reply_text("Сохраняю лог перед очисткой...")
            await self.call_get_log(update, context)

            # Затем очищаем его
            with open("main.log", "w") as f:
                f.write(f"--- Лог очищен {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

            # Сбрасываем позицию
            self.last_log_position = 0

            await update.message.reply_text("Лог успешно очищен")
            logger.info(f"Пользователь {user.id} очистил лог")
        except Exception as e:
            logger.error(f"Ошибка при очистке лога: {e}")
            await update.message.reply_text(f"Ошибка при очистке лога: {e}")

    async def call_get_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду получения лога.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка получения лога от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка получения лога от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            await self.bot.send_document(chat_id=user.id, document=open("main.log", "rb"))
            logger.info(f"Пользователь {user.id} запросил лог")
        except Exception as e:
            logger.error(f"Ошибка при отправке лога: {e}")
            await update.message.reply_text(f"Ошибка при отправке лога: {e}")

    async def create_tag(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду создания тега.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка создания тега от неразрешенного пользователя: {user.id}")
            return

        try:
            args = context.args
            if len(args) < 3:
                await update.message.reply_text(
                    "Использование: /newtag <название> <начало> <конец>\n"
                    "Пример: /newtag Работа 09:00 18:00"
                )
                return

            name = args[0]
            start_time = args[1]
            end_time = args[2]

            if not validate_time(start_time) or not validate_time(end_time):
                await update.message.reply_text("❌ Неверный формат времени. Используйте HH:MM")
                return

            if self.db.create_tag(user.id, name, start_time, end_time):
                await update.message.reply_text(f"✅ Тег '{name}' успешно создан!")
                logger.info(f"Пользователь {user.id} создал тег '{name}'")
            else:
                await update.message.reply_text("❌ Ошибка при создании тега")

        except Exception as e:
            logger.error(f"Ошибка при создании тега: {e}")
            await update.message.reply_text("Произошла ошибка при создании тега")

    async def allow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду предоставления доступа пользователю.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка предоставления доступа от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка предоставления доступа от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            args = context.args
            if len(args) != 1:
                await update.message.reply_text("Использование: /allow <telegram_id>")
                return

            telegram_id = args[0]
            if not telegram_id.isdigit():
                await update.message.reply_text("Telegram ID должен быть числом")
                return

            target_user = self.db.get_user(telegram_id)
            if not target_user:
                await update.message.reply_text(f"Пользователь с ID {telegram_id} не найден")
                return

            if self.db.update_user_permission(telegram_id, True):
                # Обновляем кэш
                cache_key = f"user_{telegram_id}"
                self.user_cache[cache_key] = True

                await update.message.reply_text(f"✅ Доступ пользователю '{telegram_id}' успешно предоставлен!")
                logger.info(f"Пользователь {user.id} предоставил доступ пользователю {telegram_id}")
            else:
                await update.message.reply_text("❌ Ошибка при предоставлении доступа")

        except Exception as e:
            logger.error(f"Ошибка при предоставлении доступа: {e}")
            await update.message.reply_text("Произошла ошибка при предоставлении доступа")

    async def db_tasks_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду просмотра всех задач в базе данных.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка просмотра всех задач от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка просмотра всех задач от не-администратора: {user.id}")
            # Проверяем, откуда пришел запрос
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id,
                                                     text="У вас нет прав для выполнения этой команды")
            else:
                await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            all_tasks = []
            users = self.db.list_users()

            for db_user in users:
                tasks = self.db.list_uncompleted_reminders(db_user["telegram_id"])
                for task in tasks:
                    task_info = {
                        "id": task["id"],
                        "user": db_user["full_name"],
                        "text": task["text"],
                        "due_time": short_format_datetime(parse_timestamp(task["due_time"])),
                        "tag": task["tag_id"]
                    }
                    all_tasks.append(task_info)

            # Проверяем, не вышли ли за пределы списка
            page_size = 5
            if self.db_tasks_listing_page * page_size >= len(all_tasks):
                self.db_tasks_listing_page = 0

            # Сортируем задачи по времени
            all_tasks.sort(key=lambda x: x["due_time"])

            # Подготавливаем страницу результатов
            start_idx = self.db_tasks_listing_page * page_size
            end_idx = min(start_idx + page_size, len(all_tasks))
            page_tasks = all_tasks[start_idx:end_idx]

            if not page_tasks:
                # Отправляем сообщение в зависимости от типа запроса
                if update.callback_query:
                    await update.callback_query.message.edit_text("📋 Задачи не найдены")
                else:
                    await update.message.reply_text("📋 Задачи не найдены")
                return

            # Формируем ответ
            response_lines = [
                f"📋 Все напоминания (стр. {self.db_tasks_listing_page + 1}/{(len(all_tasks) - 1) // page_size + 1}):"]
            for task in page_tasks:
                response_lines.append(
                    f"• {task['text']} ({task['due_time']}) [{task['tag']}] - {task['user']}"
                )

            response = "\n".join(response_lines)
            self.db_tasks_listing_page += 1

            # Добавляем кнопки навигации
            keyboard = [
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="db_tasks_prev"),
                    InlineKeyboardButton("Далее ➡️", callback_data="db_tasks_next")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Отправляем или обновляем сообщение в зависимости от типа запроса
            if update.callback_query:
                await update.callback_query.message.edit_text(response, reply_markup=reply_markup)
            else:
                await update.message.reply_text(response, reply_markup=reply_markup)

            logger.info(f"Пользователь {user.id} просмотрел страницу задач {self.db_tasks_listing_page}")

        except Exception as e:
            logger.error(f"Ошибка при просмотре задач: {e}")
            # Обрабатываем ошибку в зависимости от типа запроса
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")
            else:
                await update.message.reply_text("Произошла ошибка при получении списка задач")

    async def db_tasks_navigation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает навигацию по списку задач.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка навигации по задачам от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка навигации по задачам от не-администратора: {user.id}")
            await self.bot.answer_callback_query(update.callback_query.id, text="У вас нет прав для этой операции")
            return

        query = update.callback_query
        direction = query.data.split("_")[-1]

        try:
            if direction == "prev":
                self.db_tasks_listing_page = max(0, self.db_tasks_listing_page - 2)  # -2 т.к. в db_tasks_list будет +1

            # Запускаем обновление списка
            await self.db_tasks_list(update, context)
            await self.bot.answer_callback_query(query.id)

        except Exception as e:
            logger.error(f"Ошибка при навигации по задачам: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def user_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду просмотра списка пользователей.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка просмотра списка пользователей от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка просмотра списка пользователей от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            keyboard = []
            users = self.db.list_users()

            if not users:
                await update.message.reply_text("📋 Пользователи не найдены")
                return

            for user_info in users:
                status = "✅" if user_info["is_allowed"] else "🆕"
                role = "👑" if user_info["is_admin"] else ""
                text = f"{status} {user_info['full_name']} ({user_info['telegram_id']}) {role}"
                callback_data = f"user_get:{user_info['telegram_id']}"
                keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "👥 Выберите пользователя для подробностей:",
                reply_markup=reply_markup
            )
            logger.info(f"Пользователь {user.id} запросил список пользователей")

        except Exception as e:
            logger.error(f"Ошибка при получении списка пользователей: {e}")
            await update.message.reply_text("Произошла ошибка при получении списка пользователей")

    async def user_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на получение информации о пользователе.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка получения информации о пользователе от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка получения информации о пользователе от не-администратора: {user.id}")
            await self.bot.answer_callback_query(update.callback_query.id,
                                                 text="У вас нет прав для выполнения этой команды")
            return

        try:
            query = update.callback_query
            telegram_id = query.data.split(":")[1]

            target_user = self.db.get_user(telegram_id)
            if not target_user:
                await self.bot.answer_callback_query(query.id, text="Пользователь не найден")
                return

            # Форматируем информацию о пользователе
            user_info = (
                f"👤 Информация о пользователе:\n\n"
                f"ID: {target_user['telegram_id']}\n"
                f"Имя: {target_user['full_name']}\n"
                f"Username: {target_user.get('username', 'Не указан')}\n"
                f"Статус: {'Активен' if target_user['is_allowed'] else 'Не активирован'}\n"
                f"Роль: {'Администратор' if target_user['is_admin'] else 'Пользователь'}\n"
            )

            # Добавляем статистику задач
            tasks = self.db.list_uncompleted_reminders(target_user['telegram_id'])
            user_info += f"Активных задач: {len(tasks)}"

            # Добавляем кнопки управления пользователем
            keyboard = []
            if user.id != int(telegram_id) and (user.id == ADMIN_ID or not target_user['is_admin']):
                action = "Заблокировать" if target_user['is_allowed'] else "Разблокировать"
                callback_data = f"user_toggle:{telegram_id}"
                keyboard.append([InlineKeyboardButton(action, callback_data=callback_data)])

                if not target_user['is_admin']:
                    keyboard.append(
                        [InlineKeyboardButton("Сделать администратором", callback_data=f"user_admin:{telegram_id}")])
                elif user.id == ADMIN_ID:
                    keyboard.append(
                        [InlineKeyboardButton("Снять права администратора", callback_data=f"user_admin:{telegram_id}")])

            if keyboard:
                reply_markup = InlineKeyboardMarkup(keyboard)
                await self.bot.send_message(user.id, user_info, reply_markup=reply_markup)
            else:
                await self.bot.send_message(user.id, user_info)

            await self.bot.answer_callback_query(query.id)
            logger.info(f"Пользователь {user.id} запросил информацию о пользователе {telegram_id}")

        except Exception as e:
            logger.error(f"Ошибка при получении информации о пользователе: {e}")
            await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def user_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на изменение статуса пользователя.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка изменения статуса от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка изменения статуса от не-администратора: {user.id}")
            await self.bot.answer_callback_query(update.callback_query.id, text="У вас нет прав для этого")
            return

        try:
            query = update.callback_query
            telegram_id = query.data.split(":")[1]

            target_user = self.db.get_user(telegram_id)
            if not target_user:
                await self.bot.answer_callback_query(query.id, text="Пользователь не найден")
                return

            # Проверяем защиту от блокировки админов
            if target_user['is_admin'] and user.id != ADMIN_ID:
                await self.bot.answer_callback_query(query.id, text="Нельзя изменять статус администратора")
                return

            # Инвертируем статус
            new_status = not target_user['is_allowed']

            if self.db.update_user_permission(telegram_id, new_status):
                # Обновляем кэш
                cache_key = f"user_{telegram_id}"
                self.user_cache[cache_key] = new_status

                action = "разблокирован" if new_status else "заблокирован"
                await self.bot.answer_callback_query(query.id, text=f"Пользователь {action}")

                # Обновляем информацию о пользователе
                context.args = [telegram_id]
                update.callback_query.data = f"user_get:{telegram_id}"
                await self.user_get(update, context)

                logger.info(f"Пользователь {user.id} изменил статус пользователя {telegram_id} на {new_status}")
            else:
                await self.bot.answer_callback_query(query.id, text="Не удалось изменить статус")

        except Exception as e:
            logger.error(f"Ошибка при изменении статуса пользователя: {e}")
            await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def user_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на изменение прав администратора.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка изменения прав от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user) or user.id != ADMIN_ID:
            logger.warning(f"Попытка изменения прав от не-администратора: {user.id}")
            await self.bot.answer_callback_query(update.callback_query.id, text="У вас нет прав для этого")
            return

        try:
            query = update.callback_query
            telegram_id = query.data.split(":")[1]

            target_user = self.db.get_user(telegram_id)
            if not target_user:
                await self.bot.answer_callback_query(query.id, text="Пользователь не найден")
                return

            # Инвертируем статус админа
            new_admin_status = not target_user['is_admin']

            # Здесь должен быть метод для обновления статуса админа, реализуйте его в Database
            if self.db.update_user_admin_status(telegram_id, new_admin_status):
                action = "получил права администратора" if new_admin_status else "лишен прав администратора"
                await self.bot.answer_callback_query(query.id, text=f"Пользователь {action}")

                # Обновляем информацию о пользователе
                context.args = [telegram_id]
                update.callback_query.data = f"user_get:{telegram_id}"
                await self.user_get(update, context)

                logger.info(
                    f"Пользователь {user.id} изменил права администратора пользователя {telegram_id} на {new_admin_status}")
            else:
                await self.bot.answer_callback_query(query.id, text="Не удалось изменить права")

        except Exception as e:
            logger.error(f"Ошибка при изменении прав администратора: {e}")
            await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def disallow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду отзыва доступа у пользователя.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка отзыва доступа от неразрешенного пользователя: {user.id}")
            return

        db_user = self.db.get_user(user.id)
        if not self.user_is_admin(db_user):
            logger.warning(f"Попытка отзыва доступа от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            args = context.args
            if len(args) != 1:
                await update.message.reply_text("Использование: /ban <telegram_id>")
                return

            telegram_id = args[0]
            if not telegram_id.isdigit():
                await update.message.reply_text("Telegram ID должен быть числом")
                return

            # Защита от самоблокировки
            if int(telegram_id) == user.id:
                await update.message.reply_text("❌ Нельзя отозвать доступ у самого себя")
                return

            # Защита от блокировки администраторов
            target_user = self.db.get_user(telegram_id)
            if not target_user:
                await update.message.reply_text(f"Пользователь с ID {telegram_id} не найден")
                return

            if target_user.get("is_admin", False) and user.id != ADMIN_ID:
                await update.message.reply_text("❌ Нельзя отозвать доступ у администратора")
                return

            if self.db.update_user_permission(telegram_id, False):
                # Обновляем кэш
                cache_key = f"user_{telegram_id}"
                self.user_cache[cache_key] = False

                await update.message.reply_text(f"✅ Доступ у пользователя {telegram_id} успешно отозван!")
                logger.info(f"Пользователь {user.id} отозвал доступ у пользователя {telegram_id}")
            else:
                await update.message.reply_text("❌ Ошибка при отзыве доступа")

        except Exception as e:
            logger.error(f"Ошибка при отзыве доступа: {e}")
            await update.message.reply_text("Произошла ошибка при отзыве доступа")

    async def list_tags(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на просмотр тегов пользователя.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка просмотра тегов от неразрешенного пользователя: {user.id}")
            return

        try:
            tags = self.db.get_user_tags(user.id)

            if not tags:
                # Добавляем кнопку для быстрого создания тегов
                keyboard = [[InlineKeyboardButton("➕ Создать тег", callback_data="create_tag")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await self.bot.send_message(
                    user.id,
                    "📋 У вас пока нет тегов. Создайте первый тег командой /newtag имя_тега время_начала время_окончания",
                    reply_markup=reply_markup
                )
                if update.callback_query:
                    await self.bot.answer_callback_query(update.callback_query.id)
                return

            # Создаем клавиатуру с тегами для управления
            keyboard = []
            for tag in tags:
                tag_info = f"{tag['name']} ({tag['start_time']}-{tag['end_time']})"
                callback_data = f"tag_edit:{tag['id']}"
                keyboard.append([InlineKeyboardButton(tag_info, callback_data=callback_data)])

            # Добавляем кнопку создания нового тега
            keyboard.append([InlineKeyboardButton("➕ Создать тег", callback_data="create_tag")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.bot.send_message(user.id, "🏷 Ваши теги:", reply_markup=reply_markup)

            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id)

            logger.info(f"Пользователь {user.id} просмотрел свои теги")

        except Exception as e:
            logger.error(f"Ошибка при просмотре тегов: {e}")
            error_message = "Произошла ошибка при получении списка тегов"

            await self.bot.send_message(user.id, error_message)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def list_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на просмотр напоминаний пользователя.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        if not self.is_tg_user_allowed(user):
            logger.warning(f"Попытка просмотра напоминаний от неразрешенного пользователя: {user.id}")
            return

        try:
            tasks = self.db.list_uncompleted_reminders(user.id)

            if not tasks:
                await self.bot.send_message(user.id, "📋 У вас пока нет напоминаний")
                if update.callback_query:
                    await self.bot.answer_callback_query(update.callback_query.id)
                return

            # Группируем задачи по дате
            grouped_tasks = {}
            today = datetime.now(SERVER_TIMEZONE).date()
            tomorrow = today + timedelta(days=1)

            for task in tasks:
                due_time = parse_timestamp(task['due_time'])

                # Определяем группу
                if due_time.date() == today:
                    date_group = "Сегодня"
                elif due_time.date() == tomorrow:
                    date_group = "Завтра"
                else:
                    date_group = format_date(due_time)

                if date_group not in grouped_tasks:
                    grouped_tasks[date_group] = []

                grouped_tasks[date_group].append(task)

            # Сортируем задачи внутри групп по времени
            for date_group in grouped_tasks:
                grouped_tasks[date_group].sort(key=lambda x: parse_timestamp(x['due_time']))

            # Формируем ответ с группировкой
            response_lines = ["📋 Ваши напоминания:"]

            for date_group in sorted(grouped_tasks.keys()):
                response_lines.append(f"\n📅 {date_group}:")

                for task in grouped_tasks[date_group]:
                    due_time = parse_timestamp(task['due_time'])
                    time_str = due_time.strftime('%H:%M')
                    response_lines.append(
                        f"• {time_str} - {task['text']} [{task['tag_id']}]"
                    )

            response = "\n".join(response_lines)

            # Добавляем кнопки фильтрации
            keyboard = [
                [InlineKeyboardButton("По тегам", callback_data="filter_tasks_by_tag")],
                [InlineKeyboardButton("По дате", callback_data="filter_tasks_by_date")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await self.bot.send_message(user.id, response, reply_markup=reply_markup)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id)

            logger.info(f"Пользователь {user.id} просмотрел свои напоминания")

        except Exception as e:
            logger.error(f"Ошибка при просмотре напоминаний: {e}")
            error_message = "Произошла ошибка при получении списка напоминаний"

            await self.bot.send_message(user.id, error_message)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на получение справки.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        help_text = (
            "🤖 Привет! Я твой умный помощник в планировании.\n\n"
            "Напиши мне, что и когда тебе нужно запланировать в свободном формате, "
            "а я превращу твой запрос в структурированные напоминания.\n\n"
            "Основные команды:\n"
            "• /start - Начать работу с ботом\n"
            "• /help - Показать эту справку\n"
            "• /newtag - Создать новый тег для группировки задач\n\n"
            "Примеры запросов:\n"
            "• Напомни мне позвонить маме завтра\n"
            "• Купить молоко и хлеб по пути домой вечером\n"
            "• Запланируй встречу с клиентом в четверг в 15:00\n\n"
            "Когда наступит время напоминания, я отправлю тебе уведомление и предложу варианты переноса задачи."
        )

        try:
            await self.bot.send_message(user.id, help_text)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id)

            logger.info(f"Пользователь {user.id} запросил справку")

        except Exception as e:
            logger.error(f"Ошибка при отправке справки: {e}")
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def set_commands(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Устанавливает команды бота в меню Telegram.

        Args:
            context: Контекст планировщика
        """
        try:
            # Команды для обычных пользователей
            user_commands = [
                BotCommand("start", "Запустить бота"),
                BotCommand("help", "Показать справку"),
                BotCommand("newtag", "Добавить новый тег"),
            ]

            # Дополнительные команды для администраторов
            admin_commands = user_commands + [
                BotCommand("allow", "Предоставить доступ пользователю"),
                BotCommand("ban", "Отозвать доступ у пользователя"),
                BotCommand("list", "Список пользователей"),
                BotCommand("dbtasks", "Просмотр всех напоминаний"),
                BotCommand("monitor", "Проверить состояние бота"),
                BotCommand("getlog", "Получить журнал работы"),
                BotCommand("clearlog", "Очистить журнал"),
            ]

            # Устанавливаем команды для всех пользователей
            await self.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

            # Устанавливаем расширенный список команд для администратора
            await self.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

            logger.info("Команды бота установлены")

        except Exception as e:
            logger.error(f"Ошибка при установке команд бота: {e}")


def main() -> None:
    """Основная функция запуска бота."""
    try:
        bot = ReminderBot()
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Обработчики команд для всех пользователей
        application.add_handler(CommandHandler("start", bot.start))
        application.add_handler(CommandHandler("newtag", bot.create_tag))
        application.add_handler(CommandHandler("help", bot.help))

        # Обработчики команд для администраторов
        application.add_handler(CommandHandler("allow", bot.allow))
        application.add_handler(CommandHandler("ban", bot.disallow))
        application.add_handler(CommandHandler("list", bot.user_list))
        application.add_handler(CommandHandler("dbtasks", bot.db_tasks_list))
        application.add_handler(CommandHandler("monitor", bot.call_monitor))
        application.add_handler(CommandHandler("getlog", bot.call_get_log))
        application.add_handler(CommandHandler("clearlog", bot.call_clear_log))

        # Обработчики сообщений и коллбэков
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
        application.add_handler(CallbackQueryHandler(bot.ignore, pattern="^ignore"))
        application.add_handler(CallbackQueryHandler(bot.confirm_task, pattern="^confirm_task:"))
        application.add_handler(CallbackQueryHandler(bot.reschedule_task, pattern="^reschedule_task:"))
        application.add_handler(CallbackQueryHandler(bot.complete_task, pattern="^complete_task:"))
        application.add_handler(CallbackQueryHandler(bot.list_tags, pattern="^list_tags"))
        application.add_handler(CallbackQueryHandler(bot.list_tasks, pattern="^list_tasks"))
        application.add_handler(CallbackQueryHandler(bot.user_get, pattern="^user_get:"))
        application.add_handler(CallbackQueryHandler(bot.user_toggle, pattern="^user_toggle:"))
        application.add_handler(CallbackQueryHandler(bot.user_admin, pattern="^user_admin:"))
        application.add_handler(CallbackQueryHandler(bot.db_tasks_navigation, pattern="^db_tasks_(prev|next)"))
        application.add_handler(CallbackQueryHandler(bot.help, pattern="^help"))

        # Планировщики
        application.job_queue.run_repeating(bot.check_reminders, interval=30)
        application.job_queue.run_repeating(bot.monitor, interval=1800)
        application.job_queue.run_daily(bot.daily, time=time(7, 0, tzinfo=SERVER_TIMEZONE))
        application.job_queue.run_repeating(bot.assist, interval=300)
        application.job_queue.run_once(bot.set_commands, 0)

        logger.info("Бот запущен")
        application.run_polling()

    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
