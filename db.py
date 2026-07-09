"""
Модуль базы данных: SQLite с таблицами, индексами, FTS5 и триггерами.
Работает в WAL-режиме для конкурентного доступа.
"""

import sqlite3
import os
import logging
from enum import IntEnum
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class ContentType(IntEnum):
    """Enum типов контента, соответствует INTEGER в SQLite."""
    PHOTO = 0
    VIDEO = 1
    VOICE = 2
    AUDIO = 3
    DOCUMENT = 4
    STICKER = 5
    GIF = 6
    ROUND_VIDEO = 7
    CUSTOM_EMOJI = 8
    TEXT = 9
    ALBUM = 10

    @classmethod
    def label(cls, value: int) -> str:
        labels = {
            0: "photo",
            1: "video",
            2: "voice",
            3: "audio",
            4: "document",
            5: "sticker",
            6: "gif",
            7: "round_video",
            8: "custom_emoji",
            9: "text",
            10: "album",
        }
        return labels.get(value, "unknown")

    @classmethod
    def from_label(cls, label: str) -> int:
        labels = {
            "photo": 0,
            "video": 1,
            "voice": 2,
            "audio": 3,
            "document": 4,
            "sticker": 5,
            "gif": 6,
            "round_video": 7,
            "custom_emoji": 8,
            "text": 9,
            "album": 10,
        }
        return labels.get(label, -1)


SCHEMA_SQL = """
-- Таблица messages
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER UNIQUE NOT NULL,
    date TEXT NOT NULL,
    content_type INTEGER NOT NULL,
    text TEXT,
    file_path TEXT,
    thumbnail_path TEXT,
    file_size INTEGER,
    file_hash TEXT,
    duration REAL,
    grouped_id INTEGER,
    width INTEGER,
    height INTEGER,
    original_chat_title TEXT,
    original_sender TEXT,
    is_forwarded INTEGER DEFAULT 0,
    forward_chat_title TEXT,
    forward_sender TEXT,
    forward_message_link TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_content_type ON messages(content_type);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);
CREATE INDEX IF NOT EXISTS idx_messages_grouped_id ON messages(grouped_id);
CREATE INDEX IF NOT EXISTS idx_messages_file_hash ON messages(file_hash);

-- Таблица tags
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    tag TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_message_id ON tags(message_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- Таблица entities
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_message_id ON entities(message_id);

-- Таблица albums
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grouped_id INTEGER UNIQUE NOT NULL,
    cover_message_id INTEGER,
    message_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_albums_grouped_id ON albums(grouped_id);

-- FTS5 виртуальная таблица
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, caption,
    content='messages',
    content_rowid='id'
);

-- Триггеры для синхронизации FTS
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, caption) VALUES (new.id, COALESCE(new.text, ''), COALESCE(new.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, caption) VALUES('delete', old.id, COALESCE(old.text, ''), COALESCE(old.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, caption) VALUES('delete', old.id, COALESCE(old.text, ''), COALESCE(old.text, ''));
    INSERT INTO messages_fts(rowid, text, caption) VALUES (new.id, COALESCE(new.text, ''), COALESCE(new.text, ''));
END;
"""

# Экранирование спецсимволов FTS5 (кавычки и звёздочка)
_FTS_ESCAPE_TABLE = str.maketrans({
    '"': '""',
    '*': ' ',
})


def _sanitize_fts_query(query: str) -> str:
    """Экранировать спецсимволы FTS5 и обернуть в кавычки."""
    if not query or not query.strip():
        return ""
    # Убираем спецсимволы, ломающие синтаксис FTS5
    sanitized = query.translate(_FTS_ESCAPE_TABLE).strip()
    if not sanitized:
        return ""
    # Оборачиваем каждое слово в кавычки для точного поиска
    words = sanitized.split()
    return " AND ".join(f'"\\"{w}\\""' for w in words)


class Database:
    """Управление соединением с SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Инициализация БД: создание таблиц, индексов, включение WAL."""
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        logger.info(f"База данных инициализирована: {self.db_path}")

    @contextmanager
    def _get_conn(self):
        """Создать новое соединение (write-safe, с row_factory)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # --- Сообщения ---

    def message_exists(self, message_id: int) -> bool:
        """Проверить, существует ли сообщение в БД."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return row is not None

    def file_hash_exists(self, file_hash: str) -> dict | None:
        """Проверить, существует ли файл с таким хешем. Возвращает dict с path или None."""
        if not file_hash:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT file_path, message_id FROM messages WHERE file_hash = ? AND file_hash IS NOT NULL LIMIT 1",
                (file_hash,),
            ).fetchone()
            return dict(row) if row else None

    def insert_message(self, data: dict) -> bool:
        """
        Вставить сообщение в БД.
        data: словарь с полями таблицы messages.
        Возвращает True если вставлено, False если уже существует.
        """
        fields = [
            "message_id", "date", "content_type", "text", "file_path",
            "thumbnail_path", "file_size", "file_hash", "duration",
            "grouped_id", "width", "height", "original_chat_title", "original_sender",
            "is_forwarded", "forward_chat_title", "forward_sender", "forward_message_link"
        ]
        placeholders = ", ".join("?" * len(fields))
        field_names = ", ".join(fields)
        values = [data.get(f) for f in fields]

        with self._get_conn() as conn:
            try:
                conn.execute(
                    f"INSERT INTO messages ({field_names}) VALUES ({placeholders})",
                    values,
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                logger.debug(f"Сообщение {data.get('message_id')} уже существует в БД")
                return False

    def insert_tags(self, message_id: int, tags: list[str]):
        """Вставить теги для сообщения."""
        if not tags:
            return
        with self._get_conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO tags (message_id, tag) VALUES (?, ?)",
                [(message_id, tag) for tag in tags],
            )
            conn.commit()

    def insert_entities(self, message_id: int, entities: list[tuple[str, str]]):
        """Вставить сущности для сообщения. entities: [(type, value), ...]."""
        if not entities:
            return
        with self._get_conn() as conn:
            conn.executemany(
                "INSERT INTO entities (message_id, entity_type, value) VALUES (?, ?, ?)",
                [(message_id, etype, evalue) for etype, evalue in entities],
            )
            conn.commit()

    def upsert_album(self, grouped_id: int, cover_message_id: int, count: int):
        """Обновить или вставить запись альбома."""
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO albums (grouped_id, cover_message_id, message_count)
                   VALUES (?, ?, ?)
                   ON CONFLICT(grouped_id) DO UPDATE SET
                       message_count = excluded.message_count,
                       cover_message_id = CASE
                           WHEN albums.cover_message_id IS NULL THEN excluded.cover_message_id
                           ELSE albums.cover_message_id
                       END""",
                (grouped_id, cover_message_id, count),
            )
            conn.commit()

    def get_max_message_id(self) -> int:
        """Получить максимальный message_id из БД (для cron-режима)."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT MAX(message_id) FROM messages").fetchone()
            return row[0] or 0

    # --- Запросы для веб-сервера ---

    def get_messages(
        self,
        cursor: int = None,
        direction: str = "older",
        per_page: int = 48,
        sort: str = "date",
        order: str = "desc",
        filter_type: int = None,
        date_from: str = None,
        date_to: str = None,
        search: str = None,
        tag: str = None,
        has_text: str = None,
    ) -> tuple[list[dict], bool, int | None]:
        """
        Получить страницу сообщений с keyset pagination.
        Возвращает: (сообщения, has_next, next_cursor).
        """
        # Валидация параметров
        order = order.lower() if order else "desc"
        if order not in ("asc", "desc"):
            order = "desc"

        sort_map = {"date": "m.date", "size": "m.file_size", "type": "m.content_type"}
        sort_field = sort_map.get(sort, "m.date")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Базовый SELECT
            select_clause = "SELECT m.*"
            from_clause = "FROM messages m"
            where_clauses = []
            params = []

            # Фильтр по тегу
            if tag:
                from_clause += " INNER JOIN tags t ON m.message_id = t.message_id"
                where_clauses.append("t.tag = ?")
                params.append(tag)

            # Поиск через FTS5 (с экранированием)
            if search:
                sanitized = _sanitize_fts_query(search)
                if sanitized:
                    from_clause += " INNER JOIN messages_fts fts ON m.id = fts.rowid"
                    where_clauses.append("messages_fts MATCH ?")
                    params.append(sanitized)

            # Фильтр по типу
            if filter_type is not None:
                where_clauses.append("m.content_type = ?")
                params.append(filter_type)

            # Фильтр по датам
            if date_from:
                where_clauses.append("m.date >= ?")
                params.append(date_from)
            if date_to:
                where_clauses.append("m.date <= ?")
                params.append(date_to)

            # Фильтр по наличию текста
            if has_text == "yes":
                where_clauses.append("m.text IS NOT NULL AND m.text != ''")
            elif has_text == "no":
                where_clauses.append("(m.text IS NULL OR m.text = '')")

            # Keyset pagination
            if cursor is not None:
                if direction == "older":
                    op = "<" if order == "desc" else ">"
                    where_clauses.append(f"m.message_id {op} ?")
                else:  # newer
                    op = ">" if order == "desc" else "<"
                    where_clauses.append(f"m.message_id {op} ?")
                params.append(cursor)

            where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
            limit = per_page + 1  # Запрашиваем на 1 больше для определения has_next

            order_clause = f"ORDER BY {sort_field} {order.upper()}, m.message_id {order.upper()}"
            query = f"{select_clause} {from_clause} {where_sql} {order_clause} LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()

            has_next = len(rows) > per_page
            messages = [dict(r) for r in rows[:per_page]]
            next_cursor = messages[-1]["message_id"] if (has_next and messages) else None

            return messages, has_next, next_cursor
        finally:
            conn.close()

    def get_message(self, message_id: int) -> dict | None:
        """Получить одно сообщение по message_id."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_album_messages(self, grouped_id: int) -> list[dict]:
        """Получить все сообщения альбома."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE grouped_id = ? ORDER BY message_id",
                (grouped_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_tags_for_message(self, message_id: int) -> list[str]:
        """Получить теги для сообщения."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT tag FROM tags WHERE message_id = ?", (message_id,)
            ).fetchall()
            return [r["tag"] for r in rows]

    def get_all_tags(self) -> list[tuple[str, int]]:
        """Получить все теги с количеством использований (тег-облако)."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT tag, COUNT(*) as cnt FROM tags GROUP BY tag ORDER BY cnt DESC"
            ).fetchall()
            return [(r["tag"], r["cnt"]) for r in rows]

    def get_stats(self) -> dict:
        """Получить статистику."""
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            total_size = conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM messages").fetchone()[0]

            by_type = conn.execute(
                "SELECT content_type, COUNT(*) as cnt, COALESCE(SUM(file_size), 0) as size "
                "FROM messages GROUP BY content_type ORDER BY content_type"
            ).fetchall()
            by_type_list = [
                {"type": r["content_type"], "label": ContentType.label(r["content_type"]),
                 "count": r["cnt"], "size": r["size"]}
                for r in by_type
            ]

            by_day = conn.execute(
                "SELECT date(date) as day, COUNT(*) as cnt "
                "FROM messages GROUP BY day ORDER BY day DESC LIMIT 30"
            ).fetchall()
            by_day_list = [{"day": r["day"], "count": r["cnt"]} for r in reversed(by_day)]

            recent = conn.execute(
                "SELECT * FROM messages ORDER BY message_id DESC LIMIT 10"
            ).fetchall()

            return {
                "total_messages": total,
                "total_size": total_size,
                "by_type": by_type_list,
                "by_day": by_day_list,
                "recent": [dict(r) for r in recent],
            }

    def delete_messages(self, message_ids: list[int]) -> list[str]:
        """
        Удалить сообщения по message_id.
        Возвращает список file_path для удаления с диска.
        """
        files_to_delete = []
        with self._get_conn() as conn:
            for mid in message_ids:
                row = conn.execute(
                    "SELECT file_path, thumbnail_path FROM messages WHERE message_id = ?",
                    (mid,),
                ).fetchone()
                if row:
                    if row["file_path"]:
                        files_to_delete.append(row["file_path"])
                    if row["thumbnail_path"]:
                        files_to_delete.append(row["thumbnail_path"])
                conn.execute("DELETE FROM messages WHERE message_id = ?", (mid,))
            conn.commit()
        return files_to_delete

    def count(self) -> int:
        """Общее количество сообщений."""
        with self._get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def reindex_fts(self):
        """Перестроить FTS индекс."""
        with self._get_conn() as conn:
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            conn.commit()
        logger.info("FTS индекс перестроен")

    def find_duplicates(self) -> list[dict]:
        """Найти дубликаты по file_hash."""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT file_hash, COUNT(*) as cnt, GROUP_CONCAT(message_id) as ids
                   FROM messages
                   WHERE file_hash IS NOT NULL
                   GROUP BY file_hash HAVING cnt > 1"""
            ).fetchall()
            return [dict(r) for r in rows]