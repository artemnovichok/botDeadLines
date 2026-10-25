import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from deadline_assistant.config import DatabaseSettings


class Database:
    def __init__(self, settings: DatabaseSettings, *, readonly: bool = False) -> None:
        url = settings.database_url.get_secret_value()
        parsed = make_url(url)
        self.is_sqlite = parsed.drivername.startswith("sqlite")
        self.transaction_lock = asyncio.Lock()
        self.readonly = readonly
        from deadline_assistant.services.coordination import Coordination

        self.coordination = Coordination(url)
        if self.is_sqlite and readonly and parsed.database and parsed.database != ":memory:":
            path = Path(parsed.database).resolve()
            url = parsed.set(
                database=f"file:{quote(str(path))}", query={"uri": "true", "mode": "ro"}
            )  # type: ignore[assignment]
        if (
            parsed.drivername.startswith("sqlite")
            and parsed.database
            and parsed.database != ":memory:"
            and not readonly
        ):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.engine = create_async_engine(url, pool_pre_ping=True, echo=False, hide_parameters=True)
        if readonly and not self.is_sqlite:
            self.engine = self.engine.execution_options(
                isolation_level="SERIALIZABLE",
                postgresql_readonly=True,
            )
        if parsed.drivername.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def sqlite_setup(connection: object, _: object) -> None:
                connection.isolation_level = None  # type: ignore[attr-defined]
                cursor = connection.cursor()  # type: ignore[attr-defined]
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                if readonly:
                    cursor.execute("PRAGMA query_only=ON")
                else:
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

            @event.listens_for(self.engine.sync_engine, "begin")
            def sqlite_begin(connection: Connection) -> None:
                # SAVEPOINT обязан входить во внешнюю транзакцию даже в legacy sqlite3.
                connection.exec_driver_sql("BEGIN" if readonly else "BEGIN IMMEDIATE")

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def lock(
        self, key: str, *, wait: bool = True, timeout: float = 30
    ) -> AbstractAsyncContextManager[None]:
        return self.coordination.lock(key, wait=wait, timeout=timeout)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        if self.is_sqlite:
            # Короткие read-modify-write транзакции одного процесса не конфликтуют при WAL.
            async with self.transaction_lock, self.sessions() as session, session.begin():
                yield session
        else:
            async with self.sessions() as session, session.begin():
                yield session

    async def check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
        await self.coordination.close()
