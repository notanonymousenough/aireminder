from datetime import datetime
from config import *

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
    return datetime.fromtimestamp(timestamp_str, tz=SERVER_TIMEZONE)


def short_format_datetime(datetime_value: datetime) -> str:
    if datetime.now(SERVER_TIMEZONE).date() == datetime_value.date():
        return f"сегодня, {SHORT_WEEKDAYS[datetime_value.weekday()]}, {datetime_value.strftime("%H:%M")}"
    elif datetime.now(SERVER_TIMEZONE).date() > datetime_value.date():
        return f"прошедшее, {datetime_value.strftime("%H:%M")}"
    elif datetime.now(SERVER_TIMEZONE).date() + timedelta(days=1) >= datetime_value.date():
        return f"завтра, {SHORT_WEEKDAYS[datetime_value.weekday()]}, {datetime_value.strftime("%H:%M")}"
    elif datetime.now(SERVER_TIMEZONE).date() + timedelta(days=6) >= datetime_value.date():
        return f"{SHORT_WEEKDAYS[datetime_value.weekday()]}., {datetime_value.strftime("%H:%M")}"
    elif datetime.now(SERVER_TIMEZONE).date().year == datetime_value.date().year:
        return f"{datetime_value.day} {SHORT_MONTHS[datetime_value.month-1]}, {datetime_value.strftime("%H:%M")}"
    return datetime_value.strftime(DT_FORMAT)
