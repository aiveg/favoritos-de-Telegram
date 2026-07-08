# ⭐ Favoritos de Telegram

Персональный архиватор избранных сообщений Telegram. Забирает все сообщения из Saved Messages, скачивает медиафайлы, сохраняет метаданные в SQLite и отображает их в локальном веб-интерфейсе, стилизованном под Telegram.

## Возможности

- **Чат-интерфейс в стиле Telegram** — свои сообщения справа, пересланные слева с аватарками-животными и ссылкой на источник
- **Автоматическая синхронизация** — watch-режим слушает новые сообщения в реальном времени, cron-режим как fallback
- **Все типы контента**: фото, видео, кружочки, голосовые, аудио, документы, стикеры, GIF, текст, альбомы
- **Полнотекстовый поиск** с подсветкой (SQLite FTS5)
- **Фильтрация** по типу контента, дате, наличию текста, тегам
- **Markdown-разметка** — жирный, курсив, код, зачёркнутый, ссылки
- **Дедупликация** файлов по SHA-256
- **Экспорт** результатов в ZIP, JSON, CSV
- **Локальное открытие файлов** — клик на размер открывает в системном приложении (macOS/Linux/Windows)
- **Копирование текста** — кнопка 📋 у каждого сообщения
- **Тёмная/светлая тема** — автоопределение или ручной переключатель
- **Альбомы** — группировка и карусель
- **Тег-облако** — автотеги из #хештегов
- **Статистика** — дашборд с графиками
- **RSS-фид**

## Требования

- Python 3.11+
- Telegram API ID и API Hash (получить на [my.telegram.org](https://my.telegram.org/apps))
- Внешний IP не требуется — сервер слушает localhost

## Быстрый старт

### 1. Клонирование

```bash
git clone https://github.com/aiveg/favoritos-de-Telegram.git
cd favoritos-de-Telegram
```

### 2. Установка зависимостей

```bash
# Через venv (рекомендуется)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Или системно (macOS)
pip3 install --break-system-packages -r requirements.txt
```

### 3. Конфигурация

Создайте файл `.env` (уже есть шаблон) и укажите ваши данные:

```env
TG_API_ID=ваш_api_id
TG_API_HASH=ваш_api_hash
TG_SESSION_NAME=my_session
MEDIA_DIR=./media
DB_PATH=./data/favorites.db
SERVER_HOST=127.0.0.1
SERVER_PORT=8080
ITEMS_PER_PAGE=48
TIMEZONE=Europe/Moscow
LOG_LEVEL=INFO
WATCH_MODE=true
AUTH_ENABLED=false
AUTH_USERNAME=admin
AUTH_PASSWORD=changeme
```

### 4. Первый запуск

При первом запуске потребуется авторизация в Telegram — введите номер телефона, код подтверждения и пароль 2FA (если включён). Сессия сохранится в файл `.session`.

```bash
# Запустить сборщик (синхронизация + watch-режим)
python3 run.py collect

# В другом терминале — веб-сервер
python3 run.py server
```

Откройте http://127.0.0.1:8080

## Использование

| Команда | Описание |
|---------|----------|
| `python3 run.py server` | Только веб-сервер |
| `python3 run.py collect` | Только сборщик (watch или cron) |
| `python3 run.py all` | Сервер + сборщик в одном процессе |
| `python3 cli.py stats` | Статистика в консоли |
| `python3 cli.py deduplicate` | Поиск дубликатов |
| `python3 cli.py export` | Экспорт (ZIP/JSON/CSV) |

## Автозагрузка

### macOS (launchd)

Создайте два plist-файла в `~/Library/LaunchAgents/`:

**`com.favoritos.server.plist`**:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.favoritos.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/путь/к/venv/bin/python3</string>
        <string>/путь/к/проекту/run.py</string>
        <string>server</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/путь/к/проекту</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/путь/к/проекту/server.log</string>
    <key>StandardErrorPath</key>
    <string>/путь/к/проекту/server_error.log</string>
</dict>
</plist>
```

**`com.favoritos.collector.plist`** — аналогично, но `ProgramArguments` с `collect`.

Затем:
```bash
launchctl load ~/Library/LaunchAgents/com.favoritos.server.plist
launchctl load ~/Library/LaunchAgents/com.favoritos.collector.plist
```

### Linux (systemd)

**`/etc/systemd/system/favoritos-server.service`**:
```ini
[Unit]
Description=Favoritos de Telegram Web Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/путь/к/проекту
ExecStart=/путь/к/venv/bin/python3 run.py server
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**`favoritos-collector.service`** — аналогично с `collect`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now favoritos-server favoritos-collector
```

### Windows (Task Scheduler)

Создайте две задачи через `taskschd.msc`:
- Действие: `Запустить программу` → `pythonw.exe` (безоконный режим)
- Аргументы: `C:\путь\к\проекту\run.py server` (и вторая задача с `collect`)
- Триггер: `При запуске системы`

Или используйте `nssm` (Non-Sucking Service Manager) для создания служб.

## Структура проекта

```
├── collector.py     — сборщик (Telethon, watch/cron)
├── server.py        — веб-сервер (FastAPI + Jinja2)
├── config.py        — загрузка конфигурации из .env
├── db.py            — SQLite, FTS5, триггеры
├── cli.py           — CLI-утилита
├── run.py           — точка входа
├── requirements.txt — зависимости
├── templates/       — Jinja2-шаблоны
├── static/          — CSS, JS
├── media/           — скачанные файлы (автосоздаётся)
└── data/            — SQLite БД (автосоздаётся)
```

## Лицензия

MIT