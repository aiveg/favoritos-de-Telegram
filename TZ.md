## Цель проекта

Персональный архиватор избранных сообщений Telegram — сервис на Python, который непрерывно отслеживает новые сообщения в Saved Messages через Telethon, сохраняет медиафайлы на диск и метаданные в SQLite, и отдаёт их через локальный веб-сервер с динамической пагинацией, сортировкой, фильтрацией и полнотекстовым поиском — без хранения полного HTML со всеми сообщениями.

## Общая архитектура

Два независимых процесса: воркер-сборщик (watch-режим с fallback-кроном) и веб-сервер (работает постоянно, слушает localhost, рендерит страницы по запросу из базы данных).

- **Основной режим сборщика — watch**: постоянно работающий воркер через `client.run_until_disconnected()` с обработчиком `events.NewMessage`, сообщения появляются в вебе мгновенно.
- **Fallback-режим**: cron/systemd timer раз в час запускает инкрементальную выгрузку пропущенных сообщений из `client.iter_messages('me')` — на случай, если watch-воркер упал.
- **Хранилище файлов**: файловая система, папка `media/<тип>/<YYYY>/<MM>/<message_id>_<original_filename или hash>` — иерархия по датам, чтобы избежать переполнения директорий.
- **Хранилище метаданных**: SQLite в WAL-режиме (`PRAGMA journal_mode=WAL`) — для конкурентного чтения (веб-сервер) и записи (сборщик) без блокировок.
- **Веб-сервер**: FastAPI, отдаёт HTML через Jinja2-шаблоны с server-side keyset pagination.
- **Конфигурация**: файл `.env` (api_id, api_hash, session_name, путь к папке медиа, путь к БД, порт веб-сервера, часовой пояс, уровень логирования).
- **CLI-утилита**: `cli.py` для обслуживания (статистика, переиндексация, дедупликация, экспорт).

## Модуль 1: сборщик (collector.py)

Задача — подключаться к Telegram под личным аккаунтом и сохранять всё новое из избранного.

### Авторизация и жизненный цикл

- Авторизация по `api_id`/`api_hash` из `.env`, сессия сохраняется в файл (`.session`), чтобы не логиниться повторно.
- **Watch-режим** (основной):
  - `client.add_event_handler(handler, events.NewMessage(chats='me'))`
  - `client.run_until_disconnected()` — постоянное подключение.
  - При получении нового сообщения — немедленное скачивание и запись в БД.
  - При разрыве соединения — автоматический реконнект с экспоненциальной задержкой.
- **Cron-режим** (fallback):
  - При каждом запуске брать `MAX(message_id)` из БД и запрашивать `iter_messages('me', min_id=last_id, reverse=True)` — только новые сообщения.
  - Завершать процесс после обработки всех сообщений.

### Определение типа контента

Тип контента хранится как целочисленный enum (`IntEnum` в Python, `INTEGER` в SQLite):

| Enum | Тип |
|---|---|
| 0 | photo |
| 1 | video |
| 2 | voice |
| 3 | audio |
| 4 | document |
| 5 | sticker |
| 6 | gif |
| 7 | round_video (кружочек) |
| 8 | custom_emoji |
| 9 | text |
| 10 | album |

Определение типа по атрибутам сообщения:
- `message.photo` → photo
- `message.video` или `DocumentAttributeVideo` без `round_message` → video
- `DocumentAttributeVideo.round_message=True` → round_video
- `DocumentAttributeAudio.voice=True` → voice
- `DocumentAttributeAudio` без `voice` → audio
- `message.sticker` → sticker
- `DocumentAttributeCustomEmoji` → custom_emoji
- `message.gif` или `DocumentAttributeAnimated` → gif
- `message.grouped_id is not None` → album (но каждый элемент альбома сохраняется как отдельная запись своего типа, связанная через grouped_id)
- Всё остальное → text

### Скачивание медиафайлов

- Скачивать в `media/<тип>/<год>/<месяц>/<message_id>_<original_filename или sha256 хеш>.<ext>`.
- Перед скачиванием вычислять SHA-256 хеш медиафайла (если доступен через `message.document` / `message.photo` без полного скачивания) и проверять, нет ли уже файла с таким хешем в БД (дедупликация на лету).
- Для фото/видео **НЕ генерировать свои thumbnail** — использовать нативные миниатюры Telegram: `client.download_media(message, file=..., thumb=...)` для thumbnail.
- Thumbnail сохранять в `media/thumbnails/<message_id>_thumb.jpg`.
- Избегать повторного скачивания: проверять существование файла на диске перед загрузкой.

### Запись в БД

Поля, записываемые в таблицу `messages`:
- `message_id`, `date` (UTC, конвертировать в часовой пояс из `.env` при отображении), `content_type` (int enum), `text`/`caption`, `file_path`, `thumbnail_path`, `file_size`, `duration` (для видео/аудио), `grouped_id` (для альбомов), `width`, `height`, `file_hash` (SHA-256), `original_chat_title` (если переслано из другого чата), `original_sender` (если известно).

Также извлекать и сохранять:
- Хештеги из текста/caption в отдельную таблицу `tags` (message_id, tag).
- Сущности (mention, url, bot_command) — в таблицу `entities` (message_id, type, value).

### Обработка ошибок

- Логировать ошибки скачивания в отдельный лог-файл (`collector_errors.log`), не прерывать обработку из-за одного сбойного сообщения.
- Обрабатывать `FloodWaitError` с ожиданием и повтором.
- Все операции записи — в транзакциях SQLite.
- Идемпотентность: повторный запуск (cron) не должен дублировать записи или файлы (`INSERT OR IGNORE` по `message_id` + проверка хеша).

## Модуль 2: веб-сервер (server.py)

Задача — рендерить страницы по запросу через FastAPI + Jinja2.

### Роуты

- `GET /` — главная страница с карточками сообщений.
- `GET /message/<message_id>` — страница отдельного сообщения (полный размер медиа, метаданные).
- `GET /media/<path:file_path>` — отдача медиафайлов напрямую с диска (с проверкой `Content-Type`).
- `GET /thumbnail/<message_id>` — отдача thumbnail.
- `GET /album/<grouped_id>` — просмотр альбома как карусели.
- `GET /stats` — dashboard со статистикой.
- `GET /export` — экспорт результатов текущего фильтра в ZIP-архив.
- `GET /export/json` — экспорт метаданных текущего фильтра в JSON/CSV.
- `GET /rss` — RSS-фид последних сохранённых сообщений.
- `GET /api/messages` — JSON API для AJAX-подгрузки (опционально, для бесшовной пагинации без перезагрузки).

Также:
- `POST /delete` — массовое удаление выбранных сообщений (и из БД, и с диска).
- `GET /login` / `POST /login` — Basic Auth (опционально, включается через `.env`).

### Параметры запроса для `GET /`

| Параметр | Описание |
|---|---|
| `cursor` | keyset pagination: `message_id` последнего элемента предыдущей страницы |
| `direction` | `newer` или `older` (по умолчанию `older` — более старые) |
| `per_page` | количество элементов на странице (по умолчанию из `.env`, можно переопределить) |
| `sort` | `date`, `size`, `type` (по умолчанию `date`) |
| `order` | `asc`, `desc` (по умолчанию `desc`) |
| `filter_type` | тип контента: `photo`, `video`, `voice`, `audio`, `document`, `sticker`, `gif`, `round_video`, `custom_emoji`, `text`, `album` |
| `date_from` | начальная дата (ISO-формат) |
| `date_to` | конечная дата (ISO-формат) |
| `search` | поисковый запрос (полнотекстовый через FTS5) |
| `tag` | фильтр по тегу |
| `has_text` | `yes`/`no` — фильтр по наличию текста/caption |

### Пагинация — keyset (cursor-based)

Использовать **keyset pagination**, а не `LIMIT/OFFSET`:
- Первый запрос (без курсора): `SELECT ... ORDER BY message_id DESC LIMIT :per_page + 1`.
- Если вернулось `per_page + 1` записей — есть следующая страница, последняя запись используется как `cursor` для следующего запроса.
- Следующий запрос: `SELECT ... WHERE message_id < :cursor ORDER BY message_id DESC LIMIT :per_page + 1`.
- Вперёд: `WHERE message_id > :cursor ORDER BY message_id ASC`.

### Сортировка и фильтрация

- Сортировка: по дате (новые/старые), по типу контента, по размеру файла.
- Фильтрация: по типу контента (enum), по диапазону дат, по наличию текста/caption, по тегам.
- Полнотекстовый поиск: через SQLite FTS5 с автообновлением индекса через триггеры.

### FTS5 — полнотекстовый поиск

- Виртуальная таблица `messages_fts` (content="messages", content_rowid="id").
- Триггеры `AFTER INSERT/UPDATE/DELETE` на таблице `messages` для автоматической синхронизации с FTS.
- Поиск по полям `text` и `caption`.

### Карточки сообщений

- Миниатюра/превью (нативные thumbnail из Telegram, загруженные сборщиком).
- Клик по карточке — открывает полный файл в зависимости от типа:
  - Фото/видео/аудио — встроенный просмотрщик (lightbox / audio player).
  - Документы — прямая ссылка на скачивание.
  - Текст — разворот карточки с полным текстом.
- Иконка типа контента на каждой карточке.
- Для альбомов — специальная карточка-карусель с индикатором количества элементов.
- Отображение первых двух строк текста (если есть) прямо на карточке.
- Отображение хештегов.
- Кнопка «Открыть в Telegram» (`tg://local_message?id=...`).

### UI

- SSR через Jinja2-шаблоны.
- Легковесный CSS (никаких фреймворков), адаптивная сетка карточек.
- Светлая/тёмная тема через CSS `prefers-color-scheme` + ручной переключатель.
- Ленивая загрузка изображений (`loading="lazy"`).
- AJAX-подгрузка для бесшовной пагинации (опционально, через небольшой JS-файл).
- Панель фильтров сверху: тип контента (иконки-кнопки), диапазон дат, поисковая строка, сортировка.

### Dashboard (`/stats`)

Отдельная страница с виджетами:
- Количество файлов каждого типа (круговая диаграмма или столбики).
- Общий объём занимаемого места.
- График сохранений по дням (простая линейная диаграмма).
- Последние добавленные файлы.
- Топ-10 тегов (тег-облако).
- Вся статистика — на чистом CSS без Chart.js (или с минимальным встроенным JS для графиков).

## Схема базы данных

### Таблица `messages`

| Поле | Тип | Описание |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Внутренний ID |
| message_id | INTEGER UNIQUE NOT NULL | ID сообщения в Telegram |
| date | TEXT NOT NULL | Дата сообщения (ISO 8601, UTC) |
| content_type | INTEGER NOT NULL | Enum типа контента (0-10) |
| text | TEXT | Текст сообщения / caption |
| file_path | TEXT | Путь к медиафайлу (относительно MEDIA_DIR) |
| thumbnail_path | TEXT | Путь к миниатюре |
| file_size | INTEGER | Размер файла в байтах |
| file_hash | TEXT | SHA-256 хеш файла (для дедупликации) |
| duration | REAL | Длительность видео/аудио в секундах |
| grouped_id | INTEGER | ID альбома (для группировки) |
| width | INTEGER | Ширина изображения/видео |
| height | INTEGER | Высота изображения/видео |
| original_chat_title | TEXT | Название исходного чата (если переслано) |
| original_sender | TEXT | Отправитель в исходном чате |
| created_at | TEXT DEFAULT CURRENT_TIMESTAMP | Когда запись добавлена в БД |

Индексы: `message_id` (UNIQUE), `content_type`, `date`, `grouped_id`, `file_hash`.

### Таблица `tags`

| Поле | Тип |
|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT |
| message_id | INTEGER REFERENCES messages(message_id) |
| tag | TEXT NOT NULL |

Индекс: `(message_id, tag)`, `(tag)`.

### Таблица `entities`

| Поле | Тип |
|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT |
| message_id | INTEGER REFERENCES messages(message_id) |
| entity_type | TEXT NOT NULL |
| value | TEXT NOT NULL |

### Таблица `albums`

| Поле | Тип |
|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT |
| grouped_id | INTEGER UNIQUE NOT NULL |
| cover_message_id | INTEGER | ID первого сообщения альбома (для обложки) |
| message_count | INTEGER | Количество элементов в альбоме |

### Виртуальная таблица `messages_fts` (FTS5)

```sql
CREATE VIRTUAL TABLE messages_fts USING fts5(
    text, caption,
    content='messages',
    content_rowid='id'
);
```

Триггеры для синхронизации:
```sql
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, caption) VALUES (new.id, new.text, new.caption);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, caption) VALUES('delete', old.id, old.text, old.caption);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, caption) VALUES('delete', old.id, old.text, old.caption);
    INSERT INTO messages_fts(rowid, text, caption) VALUES (new.id, new.text, new.caption);
END;
```

## Требования к конфигурации (.env)

```
TG_API_ID=12345
TG_API_HASH=abcdef1234567890
TG_SESSION_NAME=my_session
MEDIA_DIR=./media
DB_PATH=./data/favorites.db
SERVER_HOST=127.0.0.1
SERVER_PORT=8080
ITEMS_PER_PAGE=48
TIMEZONE=Europe/Moscow
LOG_LEVEL=INFO
WATCH_MODE=true              # включить watch-режим (true) или только cron (false)
AUTH_ENABLED=false           # включить Basic Auth на веб-сервере
AUTH_USERNAME=admin
AUTH_PASSWORD=changeme
```

## Дополнительные функции

- **Экспорт результатов фильтра в ZIP-архив** одним кликом (скачиваются оригиналы файлов + CSV с метаданными).
- **Экспорт метаданных в JSON/CSV** — машиночитаемый формат без файлов.
- **Просмотр альбомов** (grouped_id) как единой карусели с перелистыванием, а не отдельными карточками.
- **Тег-облако и фильтр по тегам** — автотеги по хештегам из текста сообщений, отдельная панель фильтрации по тегам.
- **Дедупликация файлов по SHA-256** — если один и тот же файл сохранён дважды, новый экземпляр не скачивается, запись ссылается на существующий файл.
- **Простая аутентификация** (Basic Auth) на веб-сервере — включается опционально через `.env`, если сервер доступен не только с localhost.
- **Dashboard со статистикой** — количество по типам, общий объём, график по дням.
- **Watch-режим** — основной режим работы сборщика, мгновенное появление сообщений в вебе.
- **Резервное копирование БД и медиа** на S3-совместимый storage по расписанию (отдельный скрипт + cron).
- **Массовое удаление** — отметить несколько карточек и удалить разом (с подтверждением, удаление и из БД, и с диска).
- **Ссылка на оригинал в Telegram** — кнопка «Открыть в Telegram» через deep-link.
- **Светлая/тёмная тема** — автоопределение через `prefers-color-scheme` + ручной переключатель.
- **Ленивая загрузка изображений** — `loading="lazy"` на всех preview.
- **Информация о пересланных сообщениях** — название исходного чата и отправитель (если доступно).
- **RSS/Atom-фид** — `/rss` отдаёт последние сохранённые сообщения.
- **CLI-утилита** — `cli.py stats` (статистика), `cli.py reindex` (перестроить FTS), `cli.py deduplicate` (найти и отметить дубликаты), `cli.py export` (экспорт в ZIP/JSON).

## Нефункциональные требования

- **Идемпотентность**: повторный запуск сборщика не должен дублировать записи или файлы.
- **Устойчивость к разрыву соединения**: watch-режим с автореконнектом, cron как fallback.
- **Производительность**: keyset pagination для стабильной скорости на любых объёмах, WAL-режим SQLite для конкурентного доступа, FTS5 для быстрого поиска.
- **Совместимость**: macOS (launchd/cron) и Ubuntu (systemd timer/cron) без изменения кода — все пути и специфика окружения только через `.env`.
- **Логирование**: все операции (скачивание, ошибки, запуск сервера) в файл с ротацией (через `logging.handlers.RotatingFileHandler`).
- **Безопасность**: Basic Auth опционально, сервер по умолчанию слушает только localhost, файлы `.env` и `.session` в `.gitignore`.