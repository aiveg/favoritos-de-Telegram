"""
Модуль конфигурации: загрузка из .env и переменных окружения.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Конфигурация приложения."""

    def __init__(self):
        # Telegram
        self.tg_api_id = int(os.getenv("TG_API_ID", "0"))
        self.tg_api_hash = os.getenv("TG_API_HASH", "")
        self.tg_session_name = os.getenv("TG_SESSION_NAME", "my_session")

        # Пути
        self.media_dir = os.getenv("MEDIA_DIR", "./media")
        self.db_path = os.getenv("DB_PATH", "./data/favorites.db")

        # Сервер
        self.server_host = os.getenv("SERVER_HOST", "127.0.0.1")
        self.server_port = int(os.getenv("SERVER_PORT", "8080"))
        self.items_per_page = int(os.getenv("ITEMS_PER_PAGE", "48"))

        # Общие
        self.timezone = os.getenv("TIMEZONE", "Europe/Moscow")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")

        # Режимы
        self.watch_mode = os.getenv("WATCH_MODE", "true").lower() == "true"

        # Аутентификация
        self.auth_enabled = os.getenv("AUTH_ENABLED", "false").lower() == "true"
        self.auth_username = os.getenv("AUTH_USERNAME", "admin")
        self.auth_password = os.getenv("AUTH_PASSWORD", "changeme")

    def resolve_path(self, path: str) -> str:
        """Разрешить относительный путь относительно рабочей директории."""
        return str(Path(path).resolve())


config = Config()