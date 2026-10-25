"""SQLite online backup (also suitable for offline restore with --replace)."""

import argparse
import os
import sqlite3
from pathlib import Path


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Согласованная копия SQLite, включая WAL")
    parser.add_argument("source", type=Path, help="Исходный файл БД")
    parser.add_argument("destination", type=Path, help="Файл копии")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Заменить БД; приложение и CLI должны быть остановлены",
    )
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if not source.is_file() or source == destination:
        parser.error("Укажите существующий источник и другой путь для копии")
    if destination.exists() and not args.replace:
        parser.error(
            "Копия уже существует. Для восстановления используйте --replace "
            "после остановки приложения"
        )
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Исходная БД не прошла проверку целостности")
            with sqlite3.connect(destination) as dst:
                src.backup(dst, pages=256)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Копия не прошла проверку целостности")
        destination.chmod(0o600)
    except Exception:
        raise SystemExit(
            "Копирование не выполнено. Проверьте пути, целостность БД и права доступа."
        ) from None
    print("Согласованная копия SQLite создана и проверена.")


if __name__ == "__main__":
    main()
