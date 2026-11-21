"""Bounded async connections; sessions never span a provider call/audio stream."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, settings


class DatabaseConfigurationError(ValueError):
    pass


def connection_url(raw: str, config: Settings) -> URL:
    if not raw.strip():
        raise DatabaseConfigurationError("Database URL is not configured.")
    try:
        url = make_url(raw.strip())
    except Exception:
        raise DatabaseConfigurationError(
            "Invalid PostgreSQL URL; check the connection guide."
        ) from None
    if (
        url.drivername not in ("postgres", "postgresql", "postgresql+psycopg")
        or not url.host
        or not url.database
    ):
        raise DatabaseConfigurationError(
            "Use a PostgreSQL connection URL with a host and database."
        )
    # Pass the raw password to SQLAlchemy rather than interpolating a URL.
    # Embedded passwords remain supported for separate migration credentials.
    if url.password in (None, "", "[YOUR-PASSWORD]"):
        password = config.DATABASE_PASSWORD.get_secret_value()
        if password:
            url = url.set(password=password)
    if url.port == 6543:
        raise DatabaseConfigurationError(
            "Transaction pooling is unsupported; use direct or session mode on port 5432."
        )
    if config.DB_SSLMODE == "disable" and url.host not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise DatabaseConfigurationError(
            "TLS can only be disabled for a local test database."
        )
    # Configuration owns TLS: URL query parameters cannot downgrade verification.
    query = dict(url.query)
    for key in (
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "options",
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "password",
        "service",
    ):
        query.pop(key, None)
    query["sslmode"] = config.DB_SSLMODE
    if config.DB_SSLMODE == "verify-full":
        query["sslrootcert"] = config.DB_SSLROOTCERT or "system"
    return url.set(drivername="postgresql+psycopg", query=query)


def is_sqlite(raw: str) -> bool:
    return raw.strip().startswith("sqlite")


def build_sqlite_engine(raw: str, config: Settings):
    """Local single-file database. Writers are serialized with BEGIN IMMEDIATE."""
    from pathlib import Path
    from sqlalchemy import event

    url = make_url(raw.strip()).set(drivername="sqlite+aiosqlite")
    if url.database and url.database != ":memory:":
        Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        url,
        connect_args={"timeout": config.DB_IO_TIMEOUT_SECONDS},
        # Models live in the "app" schema on PostgreSQL; SQLite has one namespace.
        execution_options={"schema_translate_map": {"app": None}},
        pool_size=config.DB_POOL_SIZE,
        max_overflow=config.DB_MAX_OVERFLOW,
        pool_timeout=config.DB_POOL_TIMEOUT_SECONDS,
        hide_parameters=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure(dbapi_connection, _record):
        # Let SQLAlchemy own transactions so "begin" below is the only BEGIN.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={config.DB_IO_TIMEOUT_SECONDS * 1000}")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection):
        # Take the write lock up front: concurrent calls wait instead of failing with SQLITE_BUSY.
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    from app.timing import install_sql_timing
    install_sql_timing(engine)
    return engine


def build_engine(raw: str, config: Settings, *, migration: bool = False):
    from sqlalchemy.pool import NullPool

    if is_sqlite(raw):
        return build_sqlite_engine(raw, config)

    kwargs = (
        {"poolclass": NullPool}
        if migration
        else {
            "pool_size": config.DB_POOL_SIZE,
            "max_overflow": config.DB_MAX_OVERFLOW,
            "pool_timeout": config.DB_POOL_TIMEOUT_SECONDS,
            "pool_pre_ping": True,
            "pool_recycle": config.DB_POOL_RECYCLE_SECONDS,
        }
    )
    url = connection_url(raw, config)
    connect_args = {
            "connect_timeout": config.DB_CONNECT_TIMEOUT_SECONDS,
            "options": f"-c timezone=UTC -c statement_timeout={config.DB_STATEMENT_TIMEOUT_MS}",
            "keepalives": 1, "keepalives_idle": 10,
            "keepalives_interval": 5, "keepalives_count": 3,
            "tcp_user_timeout": config.DB_IO_TIMEOUT_SECONDS * 1000,
    }
    async def connect():
        from app.db.connection import BoundedConnection
        from app.timing import measure
        with measure('db_connect'):
            connection = await BoundedConnection.connect(
                url.set(drivername='postgresql').render_as_string(hide_password=False), **connect_args)
            connection.io_timeout = config.DB_IO_TIMEOUT_SECONDS
            return connection

    engine = create_async_engine(
        url,
        async_creator=connect,
        hide_parameters=True,
        **kwargs,
    )

    from app.timing import install_sql_timing
    install_sql_timing(engine)
    return engine


class Database:
    def __init__(self, config: Settings):
        self.config = config
        self._engine = None
        self._sessions = None

    @property
    def configured(self) -> bool:
        return bool(self.config.DATABASE_URL.get_secret_value().strip())

    @property
    def is_sqlite(self) -> bool:
        return is_sqlite(self.config.DATABASE_URL.get_secret_value())

    async def prepare(self):
        """SQLite has no Alembic history: create any missing tables on startup."""
        if not self.configured or not self.is_sqlite:
            return
        from app.db.base import Base
        from app.db import models  # noqa: F401
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    @property
    def engine(self):
        if self._engine is None:
            self._engine = build_engine(
                self.config.DATABASE_URL.get_secret_value(), self.config
            )
            self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        return self._engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Commit a successful unit of work; rollback and close on any failure."""
        self.engine
        from app.timing import measure
        with measure("db_transaction"):
            async with self._sessions.begin() as session:
                yield session

    async def dispose(self):
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = self._sessions = None


database = Database(settings)


def log_database_error(exc):
    """Never log exception text: it can contain SQL parameters or credentials."""
    import logging
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from app.timing import current
    timing = current.get()
    original = getattr(exc, 'orig', None)
    pool = database._engine.pool if database._engine else None
    logging.getLogger(__name__).error(
        'database_error request=%s category=%s error_type=%s driver_type=%s sqlstate=%s invalidated=%s pool=%s',
        timing.request_id if timing else None,
        'pool_timeout' if isinstance(exc, PoolTimeout) else 'database_operation',
        type(exc).__name__, type(original).__name__ if original else None,
        getattr(original, 'sqlstate', None), getattr(exc, 'connection_invalidated', False),
        pool.status() if pool else 'not_initialized')


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request dependency; services explicitly commit their short transactions."""
    database.engine
    async with database._sessions() as session:
        try:
            yield session
        finally:
            await session.rollback()


# Reapply transaction-local tenant context after EVERY commit, including provider I/O.
from sqlalchemy import event
from sqlalchemy.orm import Session as SyncSession
@event.listens_for(SyncSession, 'after_begin')
def tenant_context(session, transaction, connection):
    if connection.dialect.name == 'sqlite':
        return  # No row-level security; queries filter by organization explicitly.
    org = session.info.get('organization_id')
    connection.execute(text("SELECT set_config('app.organization_id', :org, true)"), {'org': str(org) if org else ''})


def insert(table):
    """INSERT supporting on_conflict_do_nothing() for the configured dialect."""
    from sqlalchemy.dialects import postgresql, sqlite
    return (sqlite if database.is_sqlite else postgresql).insert(table)
