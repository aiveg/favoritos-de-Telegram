"""
Веб-сервер на FastAPI: рендеринг страниц, пагинация, фильтрация, поиск, экспорт.
"""

import os
import io
import csv
import json
import html
import zipfile
import logging
import secrets
import re
import subprocess
import platform
from datetime import datetime, timezone as dt_timezone, timedelta
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

from jinja2 import Environment, FileSystemLoader, select_autoescape

from db import Database, ContentType
from config import config

logger = logging.getLogger(__name__)

db = Database(config.db_path)
app = FastAPI(title="Favorites Archive", version="1.0.0")

templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
jinja_env = Environment(
    loader=FileSystemLoader(str(templates_dir)),
    autoescape=select_autoescape(["html", "xml"]),
    auto_reload=True,
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

security = HTTPBasic(auto_error=False)

# --- Часовой пояс ---
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


# --- Безопасный доступ к файлам (Path Traversal protection) ---
_MEDIA_REAL = os.path.realpath(config.media_dir)


def _safe_media_path(relative_path: str) -> str:
    """Построить абсолютный путь к файлу внутри media_dir, проверив выход за пределы."""
    candidate = os.path.realpath(os.path.join(config.media_dir, relative_path))
    if not candidate.startswith(_MEDIA_REAL + os.sep) and candidate != _MEDIA_REAL:
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    return candidate


# --- Аутентификация ---

def auth_required(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    if not config.auth_enabled:
        return True
    if credentials and secrets.compare_digest(credentials.username, config.auth_username) \
            and secrets.compare_digest(credentials.password, config.auth_password):
        return True
    raise HTTPException(status_code=401, detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic realm=\"Favorites Archive\""})


# --- Фильтры форматирования ---

def format_date(date_str: str | None) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return _localize_dt(dt).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return date_str or ""


def format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return ""
    if size_bytes == 0:
        return "0 Б"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size_bytes < 1024:
            return f"{size_bytes:.0f} {unit}" if unit == "Б" else f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} ТБ"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds == 0:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


jinja_env.filters["format_date"] = format_date
jinja_env.filters["format_size"] = format_size
jinja_env.filters["format_duration"] = format_duration
jinja_env.filters["content_type_label"] = ContentType.label

_ICON_MAP = {
    0: "🖼️", 1: "🎬", 2: "🎤", 3: "🎵",
    4: "📄", 5: "😜", 6: "✨", 7: "🔵",
    8: "😀", 9: "📝", 10: "📚",
}


def icon_for_filter(content_type):
    try:
        return _ICON_MAP.get(int(content_type), "📦")
    except (TypeError, ValueError):
        return "📦"


jinja_env.filters["icon_for"] = icon_for_filter

IS_LOCAL = config.server_host in ("127.0.0.1", "localhost", "0.0.0.0")


def local_file_url(relative_path: str | None) -> str | None:
    if not relative_path or not IS_LOCAL:
        return None
    abs_path = Path(config.media_dir).resolve() / relative_path
    if abs_path.exists():
        from urllib.parse import quote
        return f"/open-file?path={quote(str(abs_path))}"
    return None


jinja_env.globals["is_local"] = IS_LOCAL
jinja_env.globals["local_file_url"] = local_file_url

_URL_RE = re.compile(r'(https?://[^\s<>"\')\]]+)', re.IGNORECASE)


def linkify_filter(text: str | None) -> str:
    if not text:
        return ""
    def replace_url(match):
        url = match.group(1)
        clean_url = url.rstrip(".,;:!?")
        escaped_url = html.escape(clean_url)
        return f'<a href="{escaped_url}" target="_blank" rel="noopener noreferrer">{escaped_url}</a>'
    return _URL_RE.sub(replace_url, text)


jinja_env.filters["linkify"] = linkify_filter


def highlight_filter(text: str | None, query: str | None) -> str:
    if not text or not query:
        return text or ""
    escaped = re.escape(query.strip())
    if not escaped:
        return text
    return re.sub(
        f"({escaped})",
        r'<mark style="background:#fff3a8;color:#000;padding:0 1px;border-radius:2px">\1</mark>',
        text,
        flags=re.IGNORECASE,
    )


jinja_env.filters["highlight"] = highlight_filter

_MD_BOLD = re.compile(r'\*\*(.+?)\*\*')
_MD_ITALIC = re.compile(r'__(.+?)__')
_MD_CODE = re.compile(r'`([^`\n]+?)`')
_MD_STRIKE = re.compile(r'~~(.+?)~~')
_MD_LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


def markdown_filter(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("<", "<").replace(">", ">")
    text = _MD_BOLD.sub(r'<strong>\1</strong>', text)
    text = _MD_ITALIC.sub(r'<em>\1</em>', text)
    text = _MD_CODE.sub(r'<code>\1</code>', text)
    text = _MD_STRIKE.sub(r'<del>\1</del>', text)
    text = _MD_LINK.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', text)
    return text


jinja_env.filters["markdown"] = markdown_filter

jinja_env.filters["js_escape"] = lambda text: json.dumps(text) if text else '""'

_ANIMALS = ["🐶", "🦊", "🐰", "🐱", "🐼", "🐨", "🐯", "🐮", "🐷", "🐸", "🐵", "🦁", "🐻", "🐹", "🐧", "🦄"]


def animal_avatar(source_title: str | None) -> str:
    if not source_title:
        return "🐶"
    return _ANIMALS[abs(hash(str(source_title))) % len(_ANIMALS)]


jinja_env.globals["animal_avatar"] = animal_avatar


def render_template(name: str, context: dict) -> HTMLResponse:
    template = jinja_env.get_template(name)
    html_content = template.render(**context)
    return HTMLResponse(content=html_content)


# --- Экранирование текста для API (защита от XSS) ---

def _esc(text: str | None) -> str:
    """Экранировать HTML-сущности в строке."""
    if not text:
        return ""
    return html.escape(text)


# ==================== Роуты ====================

@app.get("/")
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
    if per_page is None:
        per_page = config.items_per_page

    loaded_up_to = request.query_params.get("loaded_up_to")
    if loaded_up_to and not cursor:
        try:
            cursor = int(loaded_up_to)
            direction = "older"
            per_page = 500
        except ValueError:
            pass

    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    try:
        messages, has_next, next_cursor = db.get_messages(
            cursor=cursor, direction=direction, per_page=per_page,
            sort=sort, order=order, filter_type=filter_type_int,
            date_from=date_from, date_to=date_to,
            search=search, tag=tag, has_text=has_text,
        )
        all_tags = db.get_all_tags()
        total = db.count()
    except Exception as e:
        logger.error(f"Ошибка БД при загрузке ленты: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")

    return render_template("index.html", {
        "request": request,
        "messages": messages,
        "has_next": has_next,
        "next_cursor": next_cursor,
        "all_tags": all_tags,
        "total": total,
        "per_page": per_page,
        "sort": sort,
        "order": order,
        "filter_type": filter_type,
        "date_from": date_from,
        "date_to": date_to,
        "search": search,
        "tag": tag,
        "has_text": has_text,
        "ContentType": ContentType,
    })


@app.get("/message/{message_id}")
async def view_message(request: Request, message_id: int, auth: bool = Depends(auth_required)):
    try:
        msg = db.get_message(message_id)
    except Exception as e:
        logger.error(f"Ошибка БД при загрузке сообщения {message_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    try:
        tags = db.get_tags_for_message(message_id)
    except Exception:
        tags = []
    return render_template("message.html", {"request": request, "message": msg, "tags": tags})


@app.get("/album/{grouped_id}")
async def view_album(request: Request, grouped_id: int, auth: bool = Depends(auth_required)):
    try:
        messages = db.get_album_messages(grouped_id)
    except Exception as e:
        logger.error(f"Ошибка БД при загрузке альбома {grouped_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")
    if not messages:
        raise HTTPException(status_code=404, detail="Альбом не найден")
    return render_template("album.html", {"request": request, "messages": messages, "grouped_id": grouped_id})


@app.get("/media/{full_path:path}")
async def serve_media(full_path: str, auth: bool = Depends(auth_required)):
    file_path = _safe_media_path(full_path)
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    ext = full_path.rsplit(".", 1)[-1].lower() if "." in full_path else "bin"
    content_types = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "mp4": "video/mp4",
        "webm": "video/webm", "ogg": "audio/ogg", "mp3": "audio/mpeg",
        "pdf": "application/pdf",
    }
    return FileResponse(file_path, media_type=content_types.get(ext, "application/octet-stream"))


@app.get("/thumbnail/{message_id}")
async def serve_thumbnail(message_id: int, auth: bool = Depends(auth_required)):
    try:
        msg = db.get_message(message_id)
    except Exception as e:
        logger.error(f"Ошибка БД при загрузке thumbnail {message_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")
    if not msg or not msg.get("thumbnail_path"):
        raise HTTPException(status_code=404, detail="Thumbnail не найден")
    thumb_path = _safe_media_path(msg["thumbnail_path"])
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail не найден на диске")
    return FileResponse(thumb_path, media_type="image/jpeg")


@app.get("/stats")
async def stats(request: Request, auth: bool = Depends(auth_required)):
    try:
        stats_data = db.get_stats()
        all_tags = db.get_all_tags()
    except Exception as e:
        logger.error(f"Ошибка БД при загрузке статистики: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")
    return render_template("stats.html", {"request": request, "stats": stats_data, "all_tags": all_tags})


@app.get("/api/album/{grouped_id}")
async def api_album(grouped_id: int, auth: bool = Depends(auth_required)):
    try:
        messages = db.get_album_messages(grouped_id)
    except Exception as e:
        logger.error(f"Ошибка БД в API альбома {grouped_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")
    result = []
    for msg in messages:
        d = dict(msg)
        d["type_label"] = ContentType.label(d.get("content_type", 9))
        result.append(d)
    return {"messages": result}


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
    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    try:
        messages, has_next, next_cursor = db.get_messages(
            cursor=cursor, direction=direction, per_page=per_page,
            sort=sort, order=order, filter_type=filter_type_int,
            date_from=date_from, date_to=date_to,
            search=search, tag=tag, has_text=has_text,
        )
    except Exception as e:
        logger.error(f"Ошибка БД в API /api/messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")

    for msg in messages:
        msg["date_formatted"] = format_date(msg.get("date"))
        msg["size_formatted"] = format_size(msg.get("file_size"))
        msg["duration_formatted"] = format_duration(msg.get("duration"))
        msg["type_label"] = ContentType.label(msg.get("content_type", 9))
        # XSS-защита: экранируем пользовательский контент
        msg["text"] = _esc(msg.get("text"))
        msg["forward_chat_title"] = _esc(msg.get("forward_chat_title"))
        msg["forward_sender"] = _esc(msg.get("forward_sender"))
        if msg.get("file_path"):
            msg["file_path"] = _esc(msg["file_path"])

    return {"messages": messages, "has_next": has_next, "next_cursor": next_cursor, "count": len(messages)}


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
    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    limit = config.export_max_items if config.export_max_items > 0 else 100000

    try:
        messages, has_more, _ = db.get_messages(
            per_page=limit, filter_type=filter_type_int,
            date_from=date_from, date_to=date_to, search=search, tag=tag,
        )
    except Exception as e:
        logger.error(f"Ошибка БД при экспорте: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["message_id", "date", "content_type", "text", "file_path",
                          "file_size", "duration", "width", "height",
                          "original_chat", "original_sender"])
        for msg in messages:
            writer.writerow([
                msg.get("message_id"), msg.get("date"),
                ContentType.label(msg.get("content_type", 9)),
                (msg.get("text") or "")[:200], msg.get("file_path"),
                msg.get("file_size"), msg.get("duration"),
                msg.get("width"), msg.get("height"),
                msg.get("original_chat_title"), msg.get("original_sender"),
            ])
        zf.writestr("metadata.csv", csv_buffer.getvalue())
        file_count = 0
        for msg in messages:
            if msg.get("file_path"):
                fpath = _safe_media_path(msg["file_path"])
                if os.path.exists(fpath):
                    basename = os.path.basename(msg["file_path"])
                    arcname = f"files/{msg['message_id']}_{basename}"
                    zf.write(fpath, arcname)
                    file_count += 1

    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=favorites_export.zip"})


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
    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    limit = config.export_max_items if config.export_max_items > 0 else 100000

    try:
        messages, _, _ = db.get_messages(
            per_page=limit, filter_type=filter_type_int,
            date_from=date_from, date_to=date_to, search=search, tag=tag,
        )
    except Exception as e:
        logger.error(f"Ошибка БД при экспорте JSON/CSV: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")

    for msg in messages:
        msg.pop("id", None)

    if format == "csv":
        output = io.StringIO()
        if messages:
            writer = csv.DictWriter(output, fieldnames=messages[0].keys())
            writer.writeheader()
            writer.writerows(messages)
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=favorites_export.csv"})

    return JSONResponse(
        content={"count": len(messages), "messages": messages},
        headers={"Content-Disposition": "attachment; filename=favorites_export.json"},
    )


@app.get("/rss")
async def rss_feed(request: Request, auth: bool = Depends(auth_required)):
    try:
        messages, _, _ = db.get_messages(per_page=20, sort="date", order="desc")
    except Exception as e:
        logger.error(f"Ошибка БД при генерации RSS: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")

    # Определяем схему (http/https) из заголовков запроса
    scheme = request.headers.get("X-Forwarded-Proto", "http")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or f"{config.server_host}:{config.server_port}"
    base_url = f"{scheme}://{host}"

    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "Favorites Archive"
    SubElement(channel, "description").text = "Saved Messages from Telegram"
    SubElement(channel, "link").text = f"{base_url}/"

    for msg in messages:
        item = SubElement(channel, "item")
        title_text = (msg.get("text") or "Без текста")[:100]
        SubElement(item, "title").text = title_text
        SubElement(item, "description").text = f"Тип: {ContentType.label(msg.get('content_type', 9))}"
        # RFC-822 дата
        try:
            dt = datetime.fromisoformat((msg.get("date") or "").replace("Z", "+00:00"))
            dt_utc = dt.astimezone(dt_timezone.utc)
            SubElement(item, "pubDate").text = dt_utc.strftime("%a, %d %b %Y %H:%M:%S GMT")
        except Exception:
            SubElement(item, "pubDate").text = msg.get("date", "")
        msg_link = f"{base_url}/message/{msg['message_id']}"
        SubElement(item, "link").text = msg_link
        SubElement(item, "guid").text = msg_link

    xml_str = minidom.parseString(tostring(rss, "utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    return StreamingResponse(io.BytesIO(xml_str), media_type="application/rss+xml; charset=utf-8")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not config.auth_enabled:
        return RedirectResponse(url="/")
    return render_template("login.html", {"request": request})


@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if not secrets.compare_digest(username, config.auth_username) or \
       not secrets.compare_digest(password, config.auth_password):
        return HTMLResponse("<h1>Неверный логин или пароль</h1><a href='/login'>Попробовать снова</a>", status_code=401)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="auth", value="1",
        max_age=config.cookie_max_age,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/delete")
async def delete_messages(request: Request, message_ids: str = Form(...), auth: bool = Depends(auth_required)):
    ids = [int(x.strip()) for x in message_ids.split(",") if x.strip()]
    try:
        files_to_delete = db.delete_messages(ids)
    except Exception as e:
        logger.error(f"Ошибка БД при удалении: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")

    for fpath in files_to_delete:
        try:
            abs_path = _safe_media_path(fpath)
        except HTTPException:
            continue
        if os.path.exists(abs_path):
            try:
                os.remove(abs_path)
            except Exception as e:
                logger.error(f"Ошибка удаления {abs_path}: {e}")
    if "application/json" in request.headers.get("accept", ""):
        return {"deleted": len(ids), "files_removed": len(files_to_delete)}
    return RedirectResponse(url="/", status_code=302)


@app.get("/open-file")
async def open_local_file(request: Request, path: str, open: str = "0", auth: bool = Depends(auth_required)):
    if not IS_LOCAL:
        raise HTTPException(status_code=403, detail="Только локально")
    abs_path = Path(path)
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not str(abs_path.resolve()).startswith(str(Path(config.media_dir).resolve())):
        raise HTTPException(status_code=403, detail="Доступ запрещён")

    name = abs_path.name
    ext = abs_path.suffix.lstrip(".") if abs_path.suffix else ""
    size = abs_path.stat().st_size
    size_formatted = format_size(size)

    ext_icons = {
        "pdf": "📕", "doc": "📘", "docx": "📘", "xls": "📗", "xlsx": "📗",
        "txt": "📄", "csv": "📊", "json": "📋", "zip": "📦", "rar": "📦",
        "jpg": "🖼️", "jpeg": "🖼️", "png": "🖼️", "gif": "🖼️", "webp": "🖼️",
        "mp4": "🎬", "mov": "🎬", "avi": "🎬", "mkv": "🎬",
        "mp3": "🎵", "ogg": "🎵", "wav": "🎵", "m4a": "🎵",
    }
    icon = ext_icons.get(ext.lower(), "📁")

    opened = False
    if open == "1":
        try:
            if platform.system() == "Darwin":
                subprocess.run(["open", str(abs_path)])
            elif platform.system() == "Windows":
                os.startfile(str(abs_path))
            else:
                subprocess.run(["xdg-open", str(abs_path)])
            opened = True
        except Exception:
            pass

    return render_template("open_file.html", {
        "request": request,
        "name": name,
        "ext": ext,
        "size_formatted": size_formatted,
        "icon": icon,
        "path": str(abs_path),
        "opened": opened,
    })


@app.get("/health")
async def health():
    return {"status": "ok"}