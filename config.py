from datetime import timedelta, timezone

DB_NAME = "reminders.db"
ALLOWED_USERS = [313049106]  # ID разрешенных пользователей
ADMIN_ID = 313049106  # ID администратора
DT_FORMAT = "%Y/%m/%d, %H:%M"
OPTIMAL_TASKS_DELTA = timedelta(minutes=30)
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

SERVER_TIMEZONE = timezone(timedelta(hours=3))