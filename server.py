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

from jinja2 import Environment, FileSystemLoader, select_autoescape

from db import Database, ContentType
from config import config

logger = logging.getLogger(__name__)

# Инициализация БД
db = Database(config.db_path)

# FastAPI приложение
app = FastAPI(title="Favorites Archive", version="1.0.0")

# Jinja2 окружение (без кеширования, напрямую)
templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
jinja_env = Environment(
    loader=FileSystemLoader(str(templates_dir)),
    autoescape=select_autoescape(["html", "xml"]),
    auto_reload=True,
)

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
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
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


# Фильтры
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

# Определяем, локальный ли запуск
IS_LOCAL = config.server_host in ("127.0.0.1", "localhost", "0.0.0.0")

def local_file_url(relative_path: str | None) -> str | None:
    """Возвращает URL для открытия локального файла."""
    if not relative_path or not IS_LOCAL:
        return None
    abs_path = Path(config.media_dir).resolve() / relative_path
    if abs_path.exists():
        from urllib.parse import quote
        return f"/open-file?path={quote(str(abs_path))}"
    return None

jinja_env.globals["is_local"] = IS_LOCAL
jinja_env.globals["local_file_url"] = local_file_url

# Фильтр для кликабельных ссылок
import re
_URL_RE = re.compile(r'(https?://[^\s<>"\')\]]+)', re.IGNORECASE)

def linkify_filter(text: str | None) -> str:
    """Делает URL в тексте кликабельными ссылками."""
    if not text:
        return ""
    def replace_url(match):
        url = match.group(1)
        # Убираем trailing пунктуацию
        clean_url = url.rstrip(".,;:!?")
        return f'<a href="{clean_url}" target="_blank" rel="noopener noreferrer">{clean_url}</a>'
    return _URL_RE.sub(replace_url, text)

jinja_env.filters["linkify"] = linkify_filter

# Фильтр подсветки поискового запроса
def highlight_filter(text: str | None, query: str | None) -> str:
    """Подсвечивает поисковый запрос жёлтым."""
    if not text or not query:
        return text or ""
    import re
    # Экранируем спецсимволы, но ищем без учёта регистра
    escaped = re.escape(query.strip())
    if not escaped:
        return text
    return re.sub(
        f"({escaped})",
        r'<mark style="background:#fff3a8;color:#000;padding:0 1px;border-radius:2px">\1</mark>',
        text,
        flags=re.IGNORECASE
    )
jinja_env.filters["highlight"] = highlight_filter


def render_template(name: str, context: dict) -> HTMLResponse:
    """Рендерит Jinja2 шаблон и возвращает HTMLResponse."""
    template = jinja_env.get_template(name)
    html = template.render(**context)
    return HTMLResponse(content=html)


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

    # Если есть loaded_up_to — загружаем всё до этого курсора (для восстановления после back из альбома)
    loaded_up_to = request.query_params.get("loaded_up_to")
    if loaded_up_to and not cursor:
        try:
            cursor = int(loaded_up_to)
            direction = "older"
            per_page = 500  # Загружаем много, чтобы покрыть всё подгруженное
        except ValueError:
            pass

    filter_type_int = None
    if filter_type:
        filter_type_int = ContentType.from_label(filter_type)
        if filter_type_int == -1:
            filter_type_int = None

    messages, has_next, next_cursor = db.get_messages(
        cursor=cursor, direction=direction, per_page=per_page,
        sort=sort, order=order, filter_type=filter_type_int,
        date_from=date_from, date_to=date_to,
        search=search, tag=tag, has_text=has_text,
    )

    all_tags = db.get_all_tags()
    total = db.count()

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
    msg = db.get_message(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    tags = db.get_tags_for_message(message_id)
    return render_template("message.html", {"request": request, "message": msg, "tags": tags})


@app.get("/album/{grouped_id}")
async def view_album(request: Request, grouped_id: int, auth: bool = Depends(auth_required)):
    messages = db.get_album_messages(grouped_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Альбом не найден")
    return render_template("album.html", {"request": request, "messages": messages, "grouped_id": grouped_id})


@app.get("/media/{full_path:path}")
async def serve_media(full_path: str, auth: bool = Depends(auth_required)):
    file_path = os.path.join(config.media_dir, full_path)
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
    msg = db.get_message(message_id)
    if not msg or not msg.get("thumbnail_path"):
        raise HTTPException(status_code=404, detail="Thumbnail не найден")
    thumb_path = os.path.join(config.media_dir, msg["thumbnail_path"])
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail не найден на диске")
    return FileResponse(thumb_path, media_type="image/jpeg")


@app.get("/stats")
async def stats(request: Request, auth: bool = Depends(auth_required)):
    stats_data = db.get_stats()
    all_tags = db.get_all_tags()
    return render_template("stats.html", {"request": request, "stats": stats_data, "all_tags": all_tags})


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

    messages, has_next, next_cursor = db.get_messages(
        cursor=cursor, direction=direction, per_page=per_page,
        sort=sort, order=order, filter_type=filter_type_int,
        date_from=date_from, date_to=date_to,
        search=search, tag=tag, has_text=has_text,
    )

    for msg in messages:
        msg["date_formatted"] = format_date(msg.get("date"))
        msg["size_formatted"] = format_size(msg.get("file_size"))
        msg["duration_formatted"] = format_duration(msg.get("duration"))
        msg["type_label"] = ContentType.label(msg.get("content_type", 9))

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

    messages, _, _ = db.get_messages(
        per_page=10000, filter_type=filter_type_int,
        date_from=date_from, date_to=date_to, search=search, tag=tag,
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["message_id","date","content_type","text","file_path","file_size","duration","width","height","original_chat","original_sender"])
        for msg in messages:
            writer.writerow([msg.get("message_id"), msg.get("date"), ContentType.label(msg.get("content_type",9)),
                (msg.get("text") or "")[:200], msg.get("file_path"), msg.get("file_size"),
                msg.get("duration"), msg.get("width"), msg.get("height"),
                msg.get("original_chat_title"), msg.get("original_sender")])
        zf.writestr("metadata.csv", csv_buffer.getvalue())
        for msg in messages:
            if msg.get("file_path"):
                fpath = os.path.join(config.media_dir, msg["file_path"])
                if os.path.exists(fpath):
                    zf.write(fpath, f"files/{os.path.basename(msg['file_path'])}")

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

    messages, _, _ = db.get_messages(
        per_page=10000, filter_type=filter_type_int,
        date_from=date_from, date_to=date_to, search=search, tag=tag,
    )

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
    response.set_cookie(key="auth", value="1", max_age=86400)
    return response


@app.post("/delete")
async def delete_messages(request: Request, message_ids: str = Form(...), auth: bool = Depends(auth_required)):
    ids = [int(x.strip()) for x in message_ids.split(",") if x.strip()]
    files_to_delete = db.delete_messages(ids)
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


@app.get("/open-file")
async def open_local_file(request: Request, path: str, auth: bool = Depends(auth_required)):
    """Открыть файл в системном приложении (macOS: open, Linux: xdg-open, Windows: start)."""
    if not IS_LOCAL:
        raise HTTPException(status_code=403, detail="Только локально")
    abs_path = Path(path)
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not str(abs_path.resolve()).startswith(str(Path(config.media_dir).resolve())):
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    import subprocess
    import platform
    try:
        if platform.system() == "Darwin":
            subprocess.run(["open", str(abs_path)])
        elif platform.system() == "Windows":
            os.startfile(str(abs_path))
        else:
            subprocess.run(["xdg-open", str(abs_path)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return HTMLResponse("<script>window.close();</script><p>Файл открыт. <a href='/'>Назад</a></p>")


@app.get("/health")
async def health():
    return {"status": "ok", "messages": db.count()}
