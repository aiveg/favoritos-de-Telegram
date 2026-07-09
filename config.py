"""
Модуль конфигурации: загрузка из .env и переменных окружения.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _get_int(key: str, default: int) -> int:
    """Безопасное получение целочисленного значения из env."""
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class Config:
    """Конфигурация приложения."""

    def __init__(self):
        # Telegram
        self.tg_api_id = _get_int("TG_API_ID", 0)
        self.tg_api_hash = os.getenv("TG_API_HASH", "")
        self.tg_session_name = os.getenv("TG_SESSION_NAME", "my_session")

        # Пути
        self.media_dir = os.getenv("MEDIA_DIR", "./media")
        self.db_path = os.getenv("DB_PATH", "./data/favorites.db")

        # Сервер
        self.server_host = os.getenv("SERVER_HOST", "127.0.0.1")
        self.server_port = _get_int("SERVER_PORT", 8080)
        self.items_per_page = _get_int("ITEMS_PER_PAGE", 48)

        # Общие
        self.timezone = os.getenv("TIMEZONE", "Europe/Moscow")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")

        # Режимы
        self.watch_mode = os.getenv("WATCH_MODE", "true").lower() == "true"

        # Аутентификация (по умолчанию включена)
        self.auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"
        self.auth_username = os.getenv("AUTH_USERNAME", "admin")
        self.auth_password = os.getenv("AUTH_PASSWORD", "changeme")

        # ---- Константы (вынесены из кода) ----
        self.max_text_length = _get_int("MAX_TEXT_LENGTH", 10000)
        self.hash_chunk_size = _get_int("HASH_CHUNK_SIZE", 65536)
        self.retry_delay_initial = _get_int("RETRY_DELAY_INITIAL", 30)
        self.retry_delay_max = _get_int("RETRY_DELAY_MAX", 900)
        self.log_max_bytes = _get_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
        self.log_backup_count = _get_int("LOG_BACKUP_COUNT", 5)
        self.cookie_max_age = _get_int("COOKIE_MAX_AGE", 86400)
        self.export_max_items = _get_int("EXPORT_MAX_ITEMS", 0)  # 0 = без ограничения

    def resolve_path(self, path: str) -> str:
        """Разрешить относительный путь относительно рабочей директории."""
        return str(Path(path).resolve())


config = Config()