import asyncio
import sqlite3
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup_sqlite.py"


async def test_backup_restore_and_overwrite_protection(tmp_path):
    original, backup = tmp_path / "original.sqlite", tmp_path / "backup.sqlite"
    with sqlite3.connect(original) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE example(value TEXT)")
        connection.execute("INSERT INTO example VALUES ('before')")

    async def copy(source, target, *options):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SCRIPT),
            str(source),
            str(target),
            *options,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return process.returncode, output.decode()

    assert (await copy(original, backup))[0] == 0
    assert backup.stat().st_mode & 0o777 == 0o600
    assert (await copy(original, backup))[0] == 2
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE example SET value='after'")
    assert (await copy(backup, original, "--replace"))[0] == 0
    with sqlite3.connect(original) as connection:
        assert connection.execute("SELECT value FROM example").fetchone()[0] == "before"
