import logging
import sqlite3
from datetime import datetime
from getpass import fallback_getpass
from typing import *
from utils import *


# Класс для работы с базой данных

class Database:
    def __init__(self, db_name: str = "reminders.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        logging.basicConfig(level=logging.INFO)

    def _create_tables(self):
        with open('schema.sql') as f:
            self.conn.executescript(f.read())
        self.conn.commit()

    # Users
    def get_user(self, telegram_id: int) -> Optional[Dict]:
        try:
            row = self.conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logging.error(f"Error getting user: {e}")
            return None

    def list_users(self) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM users"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error getting user: {e}")
            return None

    def create_user(self, user_data: Dict) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO users (telegram_id, full_name, username, is_admin, is_allowed)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_data['telegram_id'], user_data['full_name'],
                 user_data['username'], user_data.get('is_admin', False),
                 user_data.get('is_allowed', False))
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            logging.warning("User already exists")
            return False
        except sqlite3.Error as e:
            logging.error(f"Error creating user: {e}")
            return False

    def update_user_permission(self, telegram_id: int, is_allowed: bool) -> bool:
        try:
            self.conn.execute(
                "UPDATE users SET is_allowed = ? WHERE telegram_id = ?",
                (is_allowed, telegram_id)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error updating user permission: {e}")
            return False

    # def update_user_timezone(self, telegram_id: int, timezone: int) -> bool:
    #     try:
    #         self.conn.execute(
    #             "UPDATE users SET timezone = ? WHERE telegram_id = ?",
    #             (timezone, telegram_id)
    #         )
    #         self.conn.commit()
    #         return True
    #     except sqlite3.Error as e:
    #         logging.error(f"Error updating user timezone: {e}")
    #         return False

    # Tags
    def create_tag(self, user_id: int, name: str, start_time: str, end_time: str) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO tags (user_id, name, start_time, end_time)
                   VALUES (?, ?, ?, ?)""",
                (user_id, name, start_time, end_time)
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError as e:
            logging.warning(f"Tag already exists for user: {e}")
            return False
        except sqlite3.Error as e:
            logging.error(f"Error creating tag: {e}")
            return False

    def get_user_tags(self, user_id: int) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM tags WHERE user_id = ?", (user_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error getting user tags: {e}")
            return []

    # Reminders
    def create_unconfirmed_reminder(self, user_id: int, text: str, due_time: datetime, tag_id: Optional[int] = None) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO pending_reminders (user_id, text, tag_id, due_time)
                   VALUES (?, ?, ?, ?)""",
                (user_id, text, tag_id, due_time.timestamp())
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error create_unconfirmed_reminder reminder: {e}")
            return False

    # Reminders
    def list_unconfirmed_reminders(self, user_id: int) -> List[Dict]:
        try:
            rows = self.conn.execute(
                """SELECT * FROM pending_reminders where user_id=?""",
                (user_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error list_unconfirmed_reminders reminder: {e}")
            return False

    def delete_unconfirmed_reminders(self, user_id: int):
        try:
            count = self.conn.execute(
                """DELETE FROM pending_reminders where user_id=?""",
                (user_id,)
            ).rowcount
            self.conn.commit()
            return count
        except sqlite3.Error as e:
            logging.error(f"Error delete_unconfirmed_reminders reminder: {e}")
            return False

    def delete_unconfirmed_reminder(self, task_id: str):
        try:
            self.conn.execute(
                """DELETE FROM pending_reminders where id=?""",
                (task_id,)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error delete_unconfirmed_reminder reminder: {e}")
            return False

    def get_unconfirmed_reminder(self, task_id: str) -> Dict:
        try:
            row = self.conn.execute(
                """SELECT * FROM pending_reminders where id=?""",
                (task_id,)
            ).fetchone()
            return dict(row)
        except sqlite3.Error as e:
            logging.error(f"Error get_unconfirmed_reminder reminder: {e}")
            return {}

    def get_reminder(self, task_id: str) -> Dict:
        try:
            row = self.conn.execute(
                """SELECT * FROM reminders where id=?""",
                (task_id,)
            ).fetchone()
            return dict(row)
        except sqlite3.Error as e:
            logging.error(f"Error get_reminder reminder: {e}")
            return {}

    def reschedule(self, task_id: str, new_due_time: datetime) -> bool:
        try:
            self.conn.execute(
                """UPDATE reminders SET is_completed = FALSE, due_time=? WHERE id=?""",
                (new_due_time.timestamp(), task_id,)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error reschedule reminder: {e}")
            return False



    # Reminders
    def create_reminder(self, user_id: int, text: str, due_time: datetime, tag_id: Optional[int] = None) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO reminders (user_id, text, tag_id, due_time)
                   VALUES (?, ?, ?, ?)""",
                (user_id, text, tag_id, due_time.timestamp())
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error creating reminder: {e}")
            return False

    # Reminders
    def mark_reminder_completed(self, task_id: int) -> bool:
        try:
            self.conn.execute(
                """UPDATE reminders SET is_completed = TRUE WHERE id=?""",
                (task_id,)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error mark_reminder_completed reminder: {e}")
            return False

    # Reminders
    def list_uncompleted_reminders(self, user_id: int) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE is_completed = FALSE and user_id=? ORDER BY due_time ASC", (user_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error listing reminder: {e}")
            return False

    # Reminders
    def list_reminders_by_tag(self, user_id: int, tag_id: str) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE user_id=? and tag_id=?", (user_id, tag_id)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error listing reminder: {e}")
            return False

    def get_due_reminders(self, dt: datetime) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE is_completed = FALSE AND due_time <= ?", (dt.timestamp(),)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error getting due reminders: {e}")
            return []

    def update_task_assist(self, task_id, assist) -> bool:
        try:
            self.conn.execute(
                "UPDATE reminders SET assist = ? WHERE id = ?",
                (assist, task_id)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error updating assist: {e}")
            return False

    # Admin functions
    def add_pending_user(self, telegram_id: int, full_name: str, username: str) -> bool:
        try:
            self.conn.execute(
                """INSERT OR REPLACE INTO pending_users (telegram_id, full_name, username)
                   VALUES (?, ?, ?)""",
                (telegram_id, full_name, username)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error adding pending user: {e}")
            return False

    def get_pending_users(self) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM pending_users"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error getting pending users: {e}")
            return []

    # ... другие методы для CRUD операций

    def close(self):
        self.conn.close()
