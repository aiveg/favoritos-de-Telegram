### 08.07.2026 01:57 — Переписывание ТЗ с учётом предложений по улучшению
- [x] Прочитан исходный TZ.md
- [x] Сформулированы предложения по улучшению
- [x] Переписать TZ.md с учётом всех предложений
- [x] Сделать финальный коммит

### 08.07.2026 02:00 — Реализация проекта по ТЗ
- [x] Создать .env и requirements.txt
- [x] Реализовать db.py (SQLite, таблицы, индексы, FTS5, триггеры)
- [x] Реализовать collector.py (watch-режим + cron-режим)
- [x] Реализовать server.py (FastAPI, роуты, keyset pagination, фильтрация, поиск)
- [x] Реализовать cli.py
- [x] Создать Jinja2-шаблоны и статику
- [x] Установить зависимости
- [x] Протестировать
- [x] Финальный коммит

### 08.07.2026 02:15 — Исправление: полная синхронизация архива при старте
- [x] collector.py: sync_all_messages()
- [x] watch-режим: сначала sync, потом слушатель
- [x] cron-режим: sync_all_messages()
- [x] Коммит

### 08.07.2026 02:38 — Исправление: пути по дате сообщения, UI как Telegram, чистый shutdown
- [x] collector.py: путь от message.date в МСК
- [x] run.py: KeyboardInterrupt + CancelledError
- [x] index.html + style.css: лента в стиле Telegram
- [x] Коммит

### 08.07.2026 03:00 — Исправление: Jinja2 unhashable type dict
- [x] Переход на прямой Environment вместо Jinja2Templates
- [x] icon_for как фильтр, а не global
- [x] Коммит

### 08.07.2026 13:05 — Исправление: отступы между постами
- [x] gap: 0.5rem, border-radius, box-shadow
- [x] cache-buster ?v=2
- [x] Коммит

### 08.07.2026 13:29 — Исправление: альбомы кликабельны, кнопка Назад, текст в альбоме
- [x] album.html: превью фото/видео, кликабельность, history.back()
- [x] style.css: album-doc-placeholder
- [x] collector.py: реальный тип элементов альбома (не ALBUM)
- [x] БД очищена
- [x] Коммит

### 08.07.2026 15:01 — Фича: переписка как в Telegram (свои справа, пересланные слева)
- [ ] db.py: добавить поля is_forwarded, source_message_id
- [ ] collector.py: сохранять fwd_from.from_id, chat_title, source_message_id
- [ ] index.html: два стиля карточек (свои/пересланные)
- [ ] style.css: стили для своих (справа, зелёный фон) и пересланных (слева, источник)
- [ ] message.html: тоже два варианта
- [ ] Коммит