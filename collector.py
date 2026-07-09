"""
Модуль сборщика: подключение к Telegram, скачивание медиа,
сохранение метаданных в SQLite. Watch-режим + cron-режим.
"""

import os
import re
import hashlib
import logging
import asyncio
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeAnimated,
    DocumentAttributeSticker,
    DocumentAttributeCustomEmoji,
)
from telethon.errors import FloodWaitError

from db import Database, ContentType
from config import config

logger = logging.getLogger(__name__)

# Часовой пояс из конфига
try:
    _TZ = ZoneInfo(config.timezone)
except (ZoneInfoNotFoundError, KeyError):
    _TZ = ZoneInfo("Europe/Moscow")

_UTC = dt_timezone.utc


def _localize_dt(dt: datetime) -> datetime:
    """Привести datetime к UTC (если наивный) и перевести в часовой пояс конфига."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_TZ)


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Очистить имя файла от недопустимых символов."""
    name = re.sub(r'[\\/*?:"<>|]', '_', name)
    return name[:max_len]


def extract_hashtags(text: str) -> list[str]:
    """Извлечь хештеги из текста."""
    if not text:
        return []
    return re.findall(r'#(\w+)', text.lower())


def extract_entities(message) -> list[tuple[str, str]]:
    """Извлечь сущности (mention, url, bot_command) из сообщения."""
    entities = []
    if not message.entities:
        return entities
    for ent in message.entities:
        try:
            etype = type(ent).__name__.replace("MessageEntity", "").lower()
            if etype in ("mention", "url", "botcommand"):
                offset = ent.offset
                length = ent.length
                value = message.text[offset:offset + length] if message.text else ""
                entities.append((etype, value))
        except Exception:
            pass
    return entities


def get_content_type(message) -> ContentType:
    """Определить ContentType сообщения."""
    if message.photo:
        return ContentType.PHOTO

    if message.video:
        return ContentType.VIDEO

    if message.gif:
        return ContentType.GIF

    if message.sticker:
        return ContentType.STICKER

    if message.voice:
        return ContentType.VOICE

    if message.audio:
        return ContentType.AUDIO

    if message.document:
        # Разбираем атрибуты документа
        for attr in message.document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                if attr.round_message:
                    return ContentType.ROUND_VIDEO
                return ContentType.VIDEO
            if isinstance(attr, DocumentAttributeAudio):
                if attr.voice:
                    return ContentType.VOICE
                return ContentType.AUDIO
            if isinstance(attr, DocumentAttributeSticker):
                return ContentType.STICKER
            if isinstance(attr, DocumentAttributeCustomEmoji):
                return ContentType.CUSTOM_EMOJI
            if isinstance(attr, DocumentAttributeAnimated):
                return ContentType.GIF
        return ContentType.DOCUMENT

    return ContentType.TEXT


def get_original_filename(message, content_type: ContentType) -> str | None:
    """Извлечь оригинальное имя файла из сообщения."""
    if content_type in (ContentType.PHOTO, ContentType.VIDEO, ContentType.GIF,
                         ContentType.VOICE, ContentType.AUDIO,
                         ContentType.ROUND_VIDEO, ContentType.DOCUMENT):
        if message.document:
            for attr in message.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    return attr.file_name
        if message.file and hasattr(message.file, 'name') and message.file.name:
            return message.file.name
    return None


def get_file_ext(content_type: ContentType, filename: str = None) -> str:
    """Определить расширение файла."""
    if filename and '.' in filename:
        return filename.rsplit('.', 1)[-1].lower()

    ext_map = {
        ContentType.PHOTO: "jpg",
        ContentType.VIDEO: "mp4",
        ContentType.VOICE: "ogg",
        ContentType.AUDIO: "mp3",
        ContentType.DOCUMENT: "bin",
        ContentType.STICKER: "webp",
        ContentType.GIF: "mp4",
        ContentType.ROUND_VIDEO: "mp4",
        ContentType.CUSTOM_EMOJI: "webp",
    }
    return ext_map.get(content_type, "bin")


def compute_file_hash(file_path: str) -> str | None:
    """Вычислить SHA-256 хеш файла."""
    if not os.path.exists(file_path):
        return None
    sha = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(config.hash_chunk_size), b''):
            sha.update(chunk)
    return sha.hexdigest()


class Collector:
    """Сборщик сообщений из Telegram Saved Messages."""

    def __init__(self, db: Database):
        self.db = db
        self.client = TelegramClient(
            config.tg_session_name,
            config.tg_api_id,
            config.tg_api_hash,
        )

    def _build_file_path(self, content_type: ContentType, message_id: int,
                         filename: str = None, msg_date: datetime = None) -> str:
        """
        Построить путь для сохранения файла: media/<тип>/<год>/<мес>/<id>_<имя>.<ext>.
        Дата конвертируется в часовой пояс из конфига.
        """
        if msg_date:
            local_date = _localize_dt(msg_date)
        else:
            local_date = datetime.now(_TZ)
        label = ContentType.label(int(content_type))
        ext = get_file_ext(content_type, filename)
        safe_name = sanitize_filename(filename or str(message_id))
        if not safe_name.endswith(f".{ext}"):
            safe_name = f"{safe_name}.{ext}"
        rel_path = f"{label}/{local_date.year}/{local_date.month:02d}/{message_id}_{safe_name}"
        abs_path = os.path.join(config.media_dir, rel_path)
        return abs_path

    async def _download_media(self, message, content_type: ContentType,
                              message_id: int) -> tuple[str | None, str | None, str | None]:
        """
        Скачать медиафайл сообщения.
        Возвращает: (file_path, thumbnail_path, file_hash).
        file_path — относительно MEDIA_DIR.
        """
        if content_type == ContentType.TEXT:
            return None, None, None

        filename = get_original_filename(message, content_type)
        abs_path = self._build_file_path(content_type, message_id, filename, message.date)

        # Проверяем существование файла на диске — сверяем размер
        expected_size = None
        if message.file:
            expected_size = message.file.size
        elif message.document:
            expected_size = message.document.size

        if os.path.exists(abs_path):
            actual_size = os.path.getsize(abs_path)
            if expected_size and actual_size != expected_size:
                logger.warning(f"Файл {message_id} существует но размер не совпадает (ожидалось {expected_size}, получено {actual_size}) — перекачиваем")
                os.remove(abs_path)
            else:
                logger.info(f"Файл уже существует: {abs_path}")
                file_hash = compute_file_hash(abs_path)
                rel_path = os.path.relpath(abs_path, config.media_dir)
                return rel_path, None, file_hash if file_hash else None

        # Скачиваем файл
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        try:
            await self.client.download_media(message, abs_path)
            logger.info(f"Скачан файл: {abs_path}")
        except Exception as e:
            logger.error(f"Ошибка скачивания {message_id}: {e}")
            return None, None, None

        # Вычисляем хеш
        file_hash = compute_file_hash(abs_path)

        # Проверяем дедупликацию по хешу
        if file_hash:
            existing = self.db.file_hash_exists(file_hash)
            if existing and existing.get("file_path"):
                existing_path = os.path.join(config.media_dir, existing["file_path"])
                if os.path.exists(existing_path) and abs_path != existing_path:
                    # Удаляем только что скачанный дубликат
                    os.remove(abs_path)
                    logger.info(f"Найден дубликат {message_id}, используем {existing['file_path']}")
                    return os.path.relpath(existing_path, config.media_dir), None, file_hash

        # Относительный путь
        rel_path = os.path.relpath(abs_path, config.media_dir)

        # Скачиваем thumbnail (нативный из Telegram)
        thumb_path = None
        if content_type in (ContentType.PHOTO, ContentType.VIDEO,
                             ContentType.GIF, ContentType.ROUND_VIDEO):
            try:
                thumb_dir = os.path.join(config.media_dir, "thumbnails")
                os.makedirs(thumb_dir, exist_ok=True)
                thumb_abs = os.path.join(thumb_dir, f"{message_id}_thumb.jpg")
                if not os.path.exists(thumb_abs):
                    # Пробуем скачать thumbnail-версию
                    await self.client.download_media(message, thumb_abs, thumb=True)
                    if os.path.exists(thumb_abs):
                        thumb_path = os.path.relpath(thumb_abs, config.media_dir)
                        logger.debug(f"Скачан thumbnail: {thumb_abs}")
            except Exception as e:
                logger.debug(f"Не удалось скачать thumbnail для {message_id}: {e}")

        return rel_path, thumb_path, file_hash

    async def process_message(self, message) -> bool:
        """Обработать одно сообщение: определить тип, скачать, записать в БД."""
        message_id = message.id
        date_utc = message.date
        if date_utc:
            date_utc = date_utc.replace(tzinfo=_UTC).isoformat()
        else:
            date_utc = datetime.now(_UTC).isoformat()

        # Пропускаем, если уже есть
        if self.db.message_exists(message_id):
            logger.debug(f"Сообщение {message_id} уже в БД, пропускаем")
            return False

        content_type = get_content_type(message)
        text = message.text or message.message or ""
        if text and len(text) > config.max_text_length:
            text = text[:config.max_text_length]  # Ограничение длины текста

        # Скачиваем медиа
        file_path, thumb_path, file_hash = await self._download_media(
            message, content_type, message_id
        )

        # Метаданные файла
        file_size = None
        duration = None
        width = None
        height = None
        grouped_id = message.grouped_id or None

        if message.file:
            file_size = message.file.size

        if message.document:
            if not file_size:
                file_size = message.document.size
            for attr in message.document.attributes:
                if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAudio)):
                    duration = getattr(attr, 'duration', None)
                if isinstance(attr, (DocumentAttributeVideo,)):
                    width = getattr(attr, 'w', None)
                    height = getattr(attr, 'h', None)

        if message.photo:
            if not file_size:
                # Берём размер самой большой версии фото
                sizes = sorted(message.photo.sizes, key=lambda s: getattr(s, 'size', 0), reverse=True)
                if sizes:
                    file_size = getattr(sizes[0], 'size', None)
                    width = getattr(sizes[0], 'w', None)
                    height = getattr(sizes[0], 'h', None)

        if message.video:
            if not file_size:
                file_size = getattr(message.video, 'size', None)
            width = getattr(message.video, 'w', None)
            height = getattr(message.video, 'h', None)
            duration = getattr(message.video, 'duration', None)

        # Информация о пересланном
        is_forwarded = 0
        forward_chat_title = None
        forward_sender = None
        forward_message_link = None
        if message.fwd_from:
            is_forwarded = 1
            if message.fwd_from.from_name:
                forward_sender = message.fwd_from.from_name
            if hasattr(message.fwd_from, 'from_id') and message.fwd_from.from_id:
                try:
                    chat_id = message.fwd_from.from_id
                    if hasattr(chat_id, 'channel_id'):
                        # Имя канала можно попробовать получить
                        pass
                except Exception:
                    pass
            # Попробуем извлечь название чата из заголовка пересланного сообщения
            if hasattr(message.fwd_from, 'chat_title') and message.fwd_from.chat_title:
                forward_chat_title = message.fwd_from.chat_title
            # Ссылка на оригинальное сообщение
            if hasattr(message.fwd_from, 'channel_post') and message.fwd_from.channel_post:
                try:
                    chat_id_str = str(message.fwd_from.from_id.channel_id) if hasattr(message.fwd_from.from_id, 'channel_id') else None
                    if chat_id_str:
                        forward_message_link = f"https://t.me/c/{chat_id_str}/{message.fwd_from.channel_post}"
                except Exception:
                    pass

        # Вставка в БД
        data = {
            "message_id": message_id,
            "date": date_utc,
            "content_type": int(content_type),
            "text": text,
            "file_path": file_path,
            "thumbnail_path": thumb_path,
            "file_size": file_size,
            "file_hash": file_hash,
            "duration": duration,
            "grouped_id": grouped_id,
            "width": width,
            "height": height,
            "original_chat_title": None,
            "original_sender": None,
            "is_forwarded": is_forwarded,
            "forward_chat_title": forward_chat_title,
            "forward_sender": forward_sender,
            "forward_message_link": forward_message_link,
        }

        inserted = self.db.insert_message(data)
        if inserted:
            logger.info(f"Добавлено сообщение {message_id} (тип: {content_type.name})")

            # Теги
            tags = extract_hashtags(text)
            if tags:
                self.db.insert_tags(message_id, tags)

            # Сущности
            entities = extract_entities(message)
            if entities:
                self.db.insert_entities(message_id, entities)

            # Альбомы
            if grouped_id:
                # Подсчитываем уже имеющиеся сообщения альбома
                album_msgs = self.db.get_album_messages(grouped_id)
                self.db.upsert_album(grouped_id, message_id, len(album_msgs) + 1)

            return True

        return False

    async def _process_message_batch(self, gen):
        """Обработать поток сообщений (без сохранения в память)."""
        count = 0
        async for msg in gen:
            try:
                if await self.process_message(msg):
                    count += 1
            except FloodWaitError as e:
                logger.warning(f"FloodWait: ждём {e.seconds} секунд")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Ошибка обработки сообщения {msg.id}: {e}", exc_info=True)
        return count

    async def sync_all_messages(self) -> int:
        """
        Выгрузить ВСЕ сообщения из избранного (полная синхронизация).
        Использует min_id от последнего записанного в БД сообщения.
        Если БД пустая — выгружает весь архив за всё время.
        Обрабатывает сообщения потоково, не загружая всё в память.
        Возвращает количество новых сообщений.
        """
        last_id = self.db.get_max_message_id()
        if last_id > 0:
            logger.info(f"Инкрементальная синхронизация: message_id > {last_id}")
        else:
            logger.info("БД пуста — полная синхронизация ВСЕГО архива избранного за всё время")

        try:
            gen = self.client.iter_messages('me', min_id=last_id, reverse=True, limit=None)
            total_processed = await self._process_message_batch(gen)
            logger.info(f"Синхронизация завершена. Добавлено: {total_processed} новых сообщений")
        except Exception as e:
            logger.error(f"Ошибка синхронизации: {e}", exc_info=True)

        return total_processed

    async def collect_cron(self):
        """Cron-режим: докачать всё новое с последнего message_id и выйти."""
        await self.sync_all_messages()
        await self.client.disconnect()

    async def collect_watch(self):
        """
        Watch-режим: полная синхронизация + слушатель новых сообщений.
        Устойчив к разрывам соединения — переподключается с экспоненциальным backoff.
        """
        retry_delay = config.retry_delay_initial

        # Регистрируем обработчик новых сообщений один раз
        @self.client.on(events.NewMessage(chats='me'))
        async def handler(event):
            try:
                await self.process_message(event.message)
            except FloodWaitError as e:
                logger.warning(f"FloodWait: ждём {e.seconds} секунд")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Ошибка обработки нового сообщения: {e}", exc_info=True)

        # Сначала синхронизируем весь архив
        await self.sync_all_messages()

        logger.info("=== WATCH-РЕЖИМ ЗАПУЩЕН ===")

        while True:
            try:
                await self.client.run_until_disconnected()
            except ConnectionError as e:
                logger.warning(f"Соединение разорвано: {e}")
            except asyncio.CancelledError:
                logger.info("Сборщик остановлен (CancelledError)")
                break
            except Exception as e:
                logger.warning(f"Ошибка соединения: {type(e).__name__}: {e}")

            # Переподключение с экспоненциальным backoff
            logger.info(f"Переподключение через {retry_delay} секунд...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, config.retry_delay_max)

            try:
                await self.client.connect()
                # Сбрасываем состояние сессии
                await self.client.get_me()
                # Небольшая пауза чтобы сервер Telegram сбросил буфер старых пакетов
                await asyncio.sleep(5)
                # Инкрементальная синхронизация пропущенных сообщений
                await self.sync_all_messages()
                # Сбрасываем задержку после успешного переподключения
                retry_delay = config.retry_delay_initial
            except Exception as e:
                logger.error(f"Ошибка переподключения: {e}")

    async def run(self):
        """Основной метод запуска."""
        await self.client.start()
        me = await self.client.get_me()
        logger.info(f"Авторизован как: {me.first_name} (@{me.username or 'no username'})")

        if config.watch_mode:
            await self.collect_watch()
        else:
            await self.collect_cron()