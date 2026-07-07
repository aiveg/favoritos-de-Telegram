"""
CLI-утилита для обслуживания архива.
Команды: stats, reindex, deduplicate, export.
"""

import argparse
import csv
import json
import logging
import os
import sys
import zipfile

from db import Database, ContentType
from config import config

logger = logging.getLogger("cli")


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_stats():
    """Показать статистику."""
    db = Database(config.db_path)
    stats = db.get_stats()

    print("=" * 50)
    print("  СТАТИСТИКА АРХИВА")
    print("=" * 50)
    print(f"Всего сообщений:  {stats['total_messages']}")
    total_size_mb = stats['total_size'] / (1024 * 1024) if stats['total_size'] else 0
    print(f"Общий объём:      {total_size_mb:.1f} МБ")
    print(f"Путь БД:          {config.db_path}")
    print(f"Путь медиа:       {config.media_dir}")

    print("\nПо типам контента:")
    print("-" * 50)
    for row in stats["by_type"]:
        size_mb = row["size"] / (1024 * 1024) if row["size"] else 0
        print(f"  {row['label']:<15} {row['count']:>6} шт.  {size_mb:>8.1f} МБ")

    print(f"\nПо дням (последние {len(stats['by_day'])}):")
    print("-" * 50)
    for row in stats["by_day"]:
        print(f"  {row['day']}  —  {row['count']} шт.")

    print(f"\nПоследние 10 сообщений:")
    print("-" * 50)
    for msg in stats["recent"]:
        text = (msg.get("text") or "—")[:60]
        ctype = ContentType.label(msg.get("content_type", 9))
        print(f"  [{ctype}] #{msg['message_id']} {text}")


def cmd_reindex():
    """Перестроить FTS индекс."""
    db = Database(config.db_path)
    db.reindex_fts()
    print("FTS индекс успешно перестроен.")


def cmd_deduplicate():
    """Найти и показать дубликаты."""
    db = Database(config.db_path)
    dups = db.find_duplicates()
    if not dups:
        print("Дубликатов не найдено.")
        return

    print(f"Найдено {len(dups)} групп дубликатов:")
    for d in dups:
        ids = d["ids"].split(",")
        print(f"  Хеш: {d['file_hash'][:16]}... — сообщения: {', '.join(ids)} (всего {d['cnt']})")


def cmd_export(args):
    """Экспорт в ZIP или JSON."""
    db = Database(config.db_path)

    filter_type_int = None
    if args.filter_type:
        filter_type_int = ContentType.from_label(args.filter_type)
        if filter_type_int == -1:
            print(f"Неизвестный тип: {args.filter_type}")
            sys.exit(1)

    messages, _, _ = db.get_messages(
        per_page=10000,
        filter_type=filter_type_int,
        date_from=args.date_from,
        date_to=args.date_to,
    )

    print(f"Найдено сообщений для экспорта: {len(messages)}")

    if args.format == "json":
        # Убираем id
        for m in messages:
            m.pop("id", None)
        output = args.output or "export.json"
        with open(output, "w", encoding="utf-8") as f:
            json.dump({"count": len(messages), "messages": messages}, f, ensure_ascii=False, indent=2)
        print(f"Экспортировано в {output}")

    elif args.format == "csv":
        output = args.output or "export.csv"
        with open(output, "w", encoding="utf-8", newline="") as f:
            if messages:
                writer = csv.DictWriter(f, fieldnames=messages[0].keys())
                writer.writeheader()
                writer.writerows(messages)
        print(f"Экспортировано в {output}")

    elif args.format == "zip":
        output = args.output or "export.zip"
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            # CSV внутри zip
            csv_name = "metadata.csv"
            csv_path = f"/tmp/{csv_name}"
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                if messages:
                    writer = csv.DictWriter(f, fieldnames=messages[0].keys())
                    writer.writeheader()
                    writer.writerows(messages)
            zf.write(csv_path, csv_name)

            # Файлы
            file_count = 0
            for msg in messages:
                if msg.get("file_path"):
                    fpath = os.path.join(config.media_dir, msg["file_path"])
                    if os.path.exists(fpath):
                        arcname = f"files/{os.path.basename(msg['file_path'])}"
                        zf.write(fpath, arcname)
                        file_count += 1
            os.remove(csv_path)

        print(f"Экспортировано в {output} ({file_count} файлов)")


def main():
    parser = argparse.ArgumentParser(description="Утилита управления архивом Favorites")
    subparsers = parser.add_subparsers(dest="command", help="Команда")

    subparsers.add_parser("stats", help="Показать статистику")
    subparsers.add_parser("reindex", help="Перестроить FTS индекс")
    subparsers.add_parser("deduplicate", help="Найти дубликаты файлов")

    export_parser = subparsers.add_parser("export", help="Экспорт данных")
    export_parser.add_argument("--format", choices=["json", "csv", "zip"], default="zip", help="Формат экспорта")
    export_parser.add_argument("--filter-type", type=str, help="Фильтр по типу контента")
    export_parser.add_argument("--date-from", type=str, help="Начальная дата (YYYY-MM-DD)")
    export_parser.add_argument("--date-to", type=str, help="Конечная дата (YYYY-MM-DD)")
    export_parser.add_argument("--output", type=str, help="Выходной файл")

    args = parser.parse_args()
    setup_logging()

    if args.command == "stats":
        cmd_stats()
    elif args.command == "reindex":
        cmd_reindex()
    elif args.command == "deduplicate":
        cmd_deduplicate()
    elif args.command == "export":
        cmd_export(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()