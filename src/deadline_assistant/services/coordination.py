"""Crash-safe process coordination: flock on SQLite, session advisory locks on PostgreSQL."""

import asyncio
import hashlib
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


class JobBusy(Exception):
    def __init__(self) -> None:
        super().__init__("Задание уже выполняется другим процессом; повторите позже")


class Coordination:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        self.directory = (
            Path(parsed.database).resolve().parent / ".deadline-locks"
            if parsed.drivername.startswith("sqlite")
            and parsed.database
            and parsed.database != ":memory:"
            else None
        )
        self.namespace = hashlib.sha256(
            str(
                Path(parsed.database or "").resolve() if self.directory else parsed.database
            ).encode()
        ).hexdigest()[:16]
        self.engine = (
            create_async_engine(url, poolclass=NullPool, hide_parameters=True)
            if parsed.drivername.startswith("postgresql")
            else None
        )
        self.held: set[tuple[asyncio.Task[object] | None, str]] = set()
        self.memory_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(
        self,
        key: str,
        *,
        wait: bool = True,
        timeout: float = 30,  # noqa: ASYNC109
    ) -> AsyncIterator[None]:
        owner = (asyncio.current_task(), key)
        if owner in self.held:
            yield
            return
        digest = hashlib.sha256(key.encode()).digest()
        deadline = asyncio.get_running_loop().time() + timeout

        async def again() -> None:
            if not wait or asyncio.get_running_loop().time() >= deadline:
                raise JobBusy
            await asyncio.sleep(0.05 + random.random() * 0.05)

        if self.engine is not None:
            number = int.from_bytes(digest[:8], "big", signed=True)
            # Dedicated connection: long-lived locks cannot exhaust the ORM connection pool.
            async with self.engine.connect() as connection:
                while not await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": number}
                ):
                    await connection.rollback()
                    await again()
                await connection.commit()
                self.held.add(owner)
                try:
                    yield
                finally:
                    self.held.discard(owner)
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": number}
                    )
                    await connection.commit()
        elif self.directory is not None:
            import fcntl

            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self.directory / f"{self.namespace}-{digest.hex()}.lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await again()
                self.held.add(owner)
                try:
                    yield
                finally:
                    self.held.discard(owner)
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            # Do not unlink: waiters must all refer to the same inode.
        else:
            lock = self.memory_locks.setdefault(key, asyncio.Lock())
            while lock.locked():
                await again()
            async with lock:
                self.held.add(owner)
                try:
                    yield
                finally:
                    self.held.discard(owner)

    @asynccontextmanager
    async def slot(self, name: str, count: int) -> AsyncIterator[None]:
        while True:
            for index in range(count):
                entered = False
                try:
                    async with self.lock(f"{name}:{index}", wait=False):
                        entered = True
                        yield
                        return
                except JobBusy:
                    if entered:
                        raise
            await asyncio.sleep(0.05 + random.random() * 0.1)

    async def close(self) -> None:
        if self.engine:
            await self.engine.dispose()
