"""
Точка входа: запуск веб-сервера или сборщика.
Использование:
  python run.py server   — запустить веб-сервер
  python run.py collect  — запустить сборщик (watch или cron)
  python run.py all      — запустить сервер и сборщик одновременно
"""

import sys
import logging
import asyncio
import threading
from logging.handlers import RotatingFileHandler

from config import config


def setup_logging(name: str, log_file: str):
    """Настройка логирования с ротацией."""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Файловый handler с ротацией
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    # Консольный handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    level = getattr(logging, config.log_level.upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def run_server():
    """Запуск веб-сервера."""
    import uvicorn
    from server import app

    logger = setup_logging("server", "server.log")
    logger.info(f"Запуск веб-сервера на http://{config.server_host}:{config.server_port}")

    uvicorn.run(
        app,
        host=config.server_host,
        port=config.server_port,
        log_level=config.log_level.lower(),
    )


def run_collector():
    """Запуск сборщика."""
    from db import Database
    from collector import Collector

    logger = setup_logging("collector", "collector.log")

    db = Database(config.db_path)
    collector = Collector(db)

    async def _run():
        await collector.run()

    mode = "watch" if config.watch_mode else "cron"
    logger.info(f"Запуск сборщика в режиме: {mode}")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Сборщик остановлен пользователем (Ctrl+C)")
    except asyncio.CancelledError:
        logger.info("Сборщик остановлен (CancelledError)")


def main():
    if len(sys.argv) < 2:
        print("Использование: python run.py [server|collect|all]")
        print("  server  — запустить веб-сервер")
        print("  collect — запустить сборщик")
        print("  all     — запустить сервер и сборщик одновременно")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "server":
        run_server()
    elif cmd == "collect":
        run_collector()
    elif cmd == "all":
        # Запускаем сервер в отдельном потоке
        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()
        # Запускаем сборщик в основном потоке
        run_collector()
    else:
        print(f"Неизвестная команда: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()