"""
Веб-сервер на FastAPI: рендеринг страниц, пагинация, фильтрация, поиск, экспорт.
"""

import os
import io
import csv
import json
import zipfile
import logging
import secrets
from datetime import datetime, timezone as dt_timezone, timedelta
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

from fastapi import FastAPI, Request, Query, HTTPException, Depends, Form
from fastapi.responses import (
    HTMLResponse,
    FileResponse,
    StreamingResponse,
    RedirectResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from db import Database, ContentType
from config import config

logger = logging.getLogger(__name__)

# Инициализация БД
db = Database(config.db_path)

# FastAPI приложение
app = FastAPI(title="Favorites Archive", version="1.0.0")

# Шаблоны
templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))

# Статика
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Basic Auth
security = HTTPBasic(auto_error=False)


def auth_required(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    """Проверка Basic Auth, если включена."""
    if not config.auth_enabled:
        return True
    if credentials and secrets.compare_digest(credentials.username, config.auth_username) \
            and secrets.compare_digest(credentials.password, config.auth_password):
        return True
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic realm=\"Favorites Archive\""},
    )


def format_date(date_str: str | None) -> str:
    """Форматировать дату в часовой пояс из конфигурации."""
    if not date_str:
        return ""
    try:
        tz = None
        # Простой парсинг ISO строки
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        # Пробуем конвертировать в часовой пояс из конфига
        # Для простоты используем UTC+3 (Moscow)
        dt = dt + timedelta(hours=3)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return date_str or ""


def format_size(size_bytes: int | None) -> str:
    """Форматировать размер в читаемый вид."""
    if not size_bytes:
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size_bytes < 1024:
            return f"{size_bytes:.0f} {unit}" if unit == "Б" else f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} ТБ"


def format_duration(seconds: float | None) -> str:
    """Форматировать длительность."""
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# Шаблонные фильтры
templates.env.filters["format_date"] = format_date
templates.env.filters["format_size"] = format_size
templates.env.filters["format_duration"] = format_duration
templates.env.filters["content_type_label"] = ContentType.label

def dict_to_query(d: dict, overrides: dict = None, exclude: list = None, except_key: str = None) -> str:
    """Формирует query string из словаря параметров."""
    from urllib.parse import urlencode
    params = dict(d)
    # Удаляем None и пустые
    params = {k: v for k, v in params.items() if v is not None and v != ""}
    # Удаляем исключённые ключи
    if exclude:
        for k in exclude:
            params.pop(k, None)
    if except_key:
        params.pop(except_key, None)
    # Применяем переопределения
    if overrides:
        for k, v in overrides.items():
            if v is None:
                params.pop(k, None)
            else:
                params[k] = v
    return urlencode(params)

templates.env.filters["dict_to_query"] = dict_to_query


# ==================== Роуты ====================

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    cursor: int | None = None,
    direction: str = "older",
    per_page: int = Query(default=None),
    sort: str = "date",
    order: str = "desc",
    filter_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    tag: str | None = None,
    has_text: str | None = None,
    auth: bool = Depends(auth_required),
):
    """Главная страница с карточками."""
    if per_page is None:
        per_page = config.items_per_page

    # Конвертация filter_type в int enum
    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    messages, has_next, next_cursor = db.get_messages(
        cursor=cursor,
        direction=direction,
        per_page=per_page,
        sort=sort,
        order=order,
        filter_type=filter_type_int,
        date_from=date_from,
        date_to=date_to,
        search=search,
        tag=tag,
        has_text=has_text,
    )

    # Предыдущий курсор (для пагинации назад)
    prev_cursor = None
    has_prev = False
    if cursor is not None:
        # Запрашиваем в обратном направлении для проверки наличия предыдущей страницы
        prev_dir = "newer" if direction == "older" else "older"
        _, has_prev, _ = db.get_messages(
            cursor=cursor,
            direction=prev_dir,
            per_page=1,
            sort=sort,
            order=order,
            filter_type=filter_type_int,
            date_from=date_from,
            date_to=date_to,
            search=search,
            tag=tag,
            has_text=has_text,
        )
        if has_prev and messages:
            prev_cursor = messages[0]["message_id"]

    # Все теги для фильтра
    all_tags = db.get_all_tags()

    # Общее количество (приблизительно)
    total = db.count()

    # Текущие параметры для URL
    query_params = {
        "per_page": per_page,
        "sort": sort,
        "order": order,
        "filter_type": filter_type,
        "date_from": date_from,
        "date_to": date_to,
        "search": search,
        "tag": tag,
        "has_text": has_text,
    }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "messages": messages,
            "has_next": has_next,
            "has_prev": has_prev,
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
            "direction": direction,
            "all_tags": all_tags,
            "total": total,
            "query_params": query_params,
            "filter_type": filter_type,
            "ContentType": ContentType,
        },
    )


@app.get("/message/{message_id}", response_class=HTMLResponse)
async def view_message(request: Request, message_id: int, auth: bool = Depends(auth_required)):
    """Страница отдельного сообщения."""
    msg = db.get_message(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    tags = db.get_tags_for_message(message_id)
    return templates.TemplateResponse(
        "message.html",
        {
            "request": request,
            "message": msg,
            "tags": tags,
        },
    )


@app.get("/album/{grouped_id}", response_class=HTMLResponse)
async def view_album(request: Request, grouped_id: int, auth: bool = Depends(auth_required)):
    """Просмотр альбома."""
    messages = db.get_album_messages(grouped_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Альбом не найден")

    return templates.TemplateResponse(
        "album.html",
        {
            "request": request,
            "messages": messages,
            "grouped_id": grouped_id,
        },
    )


@app.get("/media/{full_path:path}")
async def serve_media(full_path: str, auth: bool = Depends(auth_required)):
    """Отдача медиафайлов."""
    file_path = os.path.join(config.media_dir, full_path)
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")

    # Определяем Content-Type
    ext = full_path.rsplit(".", 1)[-1].lower() if "." in full_path else "bin"
    content_types = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "mp4": "video/mp4",
        "webm": "video/webm",
        "ogg": "audio/ogg",
        "mp3": "audio/mpeg",
        "pdf": "application/pdf",
    }
    media_type = content_types.get(ext, "application/octet-stream")

    return FileResponse(
        file_path,
        media_type=media_type,
    )


@app.get("/thumbnail/{message_id}")
async def serve_thumbnail(message_id: int, auth: bool = Depends(auth_required)):
    """Отдача thumbnail."""
    msg = db.get_message(message_id)
    if not msg or not msg.get("thumbnail_path"):
        raise HTTPException(status_code=404, detail="Thumbnail не найден")

    thumb_path = os.path.join(config.media_dir, msg["thumbnail_path"])
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail не найден на диске")

    return FileResponse(thumb_path, media_type="image/jpeg")


@app.get("/stats", response_class=HTMLResponse)
async def stats(request: Request, auth: bool = Depends(auth_required)):
    """Dashboard со статистикой."""
    stats_data = db.get_stats()
    all_tags = db.get_all_tags()
    return templates.TemplateResponse(
        "stats.html",
        {
            "request": request,
            "stats": stats_data,
            "all_tags": all_tags,
        },
    )


@app.get("/api/messages")
async def api_messages(
    request: Request,
    cursor: int | None = None,
    direction: str = "older",
    per_page: int = Query(default=48),
    sort: str = "date",
    order: str = "desc",
    filter_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    tag: str | None = None,
    has_text: str | None = None,
    auth: bool = Depends(auth_required),
):
    """JSON API для AJAX-подгрузки."""
    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    messages, has_next, next_cursor = db.get_messages(
        cursor=cursor,
        direction=direction,
        per_page=per_page,
        sort=sort,
        order=order,
        filter_type=filter_type_int,
        date_from=date_from,
        date_to=date_to,
        search=search,
        tag=tag,
        has_text=has_text,
    )

    # Добавляем форматированные поля
    for msg in messages:
        msg["date_formatted"] = format_date(msg.get("date"))
        msg["size_formatted"] = format_size(msg.get("file_size"))
        msg["duration_formatted"] = format_duration(msg.get("duration"))
        msg["type_label"] = ContentType.label(msg.get("content_type", 9))

    return {
        "messages": messages,
        "has_next": has_next,
        "next_cursor": next_cursor,
        "count": len(messages),
    }


@app.get("/export")
async def export_zip(
    request: Request,
    filter_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    tag: str | None = None,
    auth: bool = Depends(auth_required),
):
    """Экспорт результатов фильтра в ZIP-архив."""
    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    # Получаем все сообщения по фильтру (без пагинации, до 10000)
    messages, _, _ = db.get_messages(
        per_page=10000,
        filter_type=filter_type_int,
        date_from=date_from,
        date_to=date_to,
        search=search,
        tag=tag,
    )

    # Создаём ZIP в памяти
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # CSV с метаданными
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow([
            "message_id", "date", "content_type", "text", "file_path",
            "file_size", "duration", "width", "height", "original_chat", "original_sender"
        ])
        for msg in messages:
            writer.writerow([
                msg.get("message_id"),
                msg.get("date"),
                ContentType.label(msg.get("content_type", 9)),
                (msg.get("text") or "")[:200],
                msg.get("file_path"),
                msg.get("file_size"),
                msg.get("duration"),
                msg.get("width"),
                msg.get("height"),
                msg.get("original_chat_title"),
                msg.get("original_sender"),
            ])
        zf.writestr("metadata.csv", csv_buffer.getvalue())

        # Добавляем файлы
        for msg in messages:
            if msg.get("file_path"):
                fpath = os.path.join(config.media_dir, msg["file_path"])
                if os.path.exists(fpath):
                    arcname = f"files/{os.path.basename(msg['file_path'])}"
                    zf.write(fpath, arcname)

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=favorites_export.zip"},
    )


@app.get("/export/json")
async def export_json(
    request: Request,
    filter_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    tag: str | None = None,
    format: str = "json",
    auth: bool = Depends(auth_required),
):
    """Экспорт метаданных в JSON или CSV."""
    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    messages, _, _ = db.get_messages(
        per_page=10000,
        filter_type=filter_type_int,
        date_from=date_from,
        date_to=date_to,
        search=search,
        tag=tag,
    )

    # Убираем внутренний id
    for msg in messages:
        msg.pop("id", None)

    if format == "csv":
        output = io.StringIO()
        if messages:
            writer = csv.DictWriter(output, fieldnames=messages[0].keys())
            writer.writeheader()
            writer.writerows(messages)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=favorites_export.csv"},
        )

    return JSONResponse(
        content={"count": len(messages), "messages": messages},
        headers={"Content-Disposition": "attachment; filename=favorites_export.json"},
    )


@app.get("/rss")
async def rss_feed(request: Request, auth: bool = Depends(auth_required)):
    """RSS-фид последних сообщений."""
    messages, _, _ = db.get_messages(per_page=20, sort="date", order="desc")

    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "Favorites Archive"
    SubElement(channel, "description").text = "Saved Messages from Telegram"
    SubElement(channel, "link").text = f"http://{config.server_host}:{config.server_port}/"

    for msg in messages:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = (msg.get("text") or "Без текста")[:100]
        SubElement(item, "description").text = f"Тип: {ContentType.label(msg.get('content_type', 9))}"
        SubElement(item, "pubDate").text = msg.get("date", "")
        msg_link = f"http://{config.server_host}:{config.server_port}/message/{msg['message_id']}"
        SubElement(item, "link").text = msg_link
        SubElement(item, "guid").text = msg_link

    xml_str = minidom.parseString(tostring(rss, "utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    return StreamingResponse(
        io.BytesIO(xml_str),
        media_type="application/rss+xml; charset=utf-8",
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Страница входа."""
    if not config.auth_enabled:
        return RedirectResponse(url="/")
    return templates.TemplateResponse(
        "login.html",
        {"request": request},
    )


@app.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Обработка входа."""
    if not secrets.compare_digest(username, config.auth_username) or \
       not secrets.compare_digest(password, config.auth_password):
        return HTMLResponse("<h1>Неверный логин или пароль</h1><a href='/login'>Попробовать снова</a>", status_code=401)

    response = RedirectResponse(url="/", status_code=302)
    # Устанавливаем cookie для простой аутентификации
    response.set_cookie(key="auth", value="1", max_age=86400)
    return response


@app.post("/delete")
async def delete_messages(
    request: Request,
    message_ids: str = Form(...),
    auth: bool = Depends(auth_required),
):
    """Массовое удаление сообщений."""
    ids = [int(x.strip()) for x in message_ids.split(",") if x.strip()]
    files_to_delete = db.delete_messages(ids)

    # Удаляем файлы с диска
    for fpath in files_to_delete:
        abs_path = os.path.join(config.media_dir, fpath)
        if os.path.exists(abs_path):
            try:
                os.remove(abs_path)
            except Exception as e:
                logger.error(f"Ошибка удаления {abs_path}: {e}")

    if "application/json" in request.headers.get("accept", ""):
        return {"deleted": len(ids), "files_removed": len(files_to_delete)}

    return RedirectResponse(url="/", status_code=302)


# ==================== Health check ====================

@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok", "messages": db.count()}