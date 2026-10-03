"""Bounded async connections; sessions never span a provider call/audio stream."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import URL, make_url
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


def build_engine(raw: str, config: Settings, *, migration: bool = False):
    from sqlalchemy.pool import NullPool

    kwargs = (
        {"poolclass": NullPool}
        if migration
        else {
            "pool_size": config.DB_POOL_SIZE,
            "max_overflow": config.DB_MAX_OVERFLOW,
            "pool_timeout": config.DB_POOL_TIMEOUT_SECONDS,
            "pool_pre_ping": True,
        }
    )
    return create_async_engine(
        connection_url(raw, config),
        connect_args={
            "connect_timeout": config.DB_CONNECT_TIMEOUT_SECONDS,
            "options": f"-c timezone=UTC -c statement_timeout={config.DB_STATEMENT_TIMEOUT_MS}",
        },
        hide_parameters=True,
        **kwargs,
    )


class Database:
    def __init__(self, config: Settings):
        self.config = config
        self._engine = None
        self._sessions = None

    @property
    def configured(self) -> bool:
        return bool(self.config.DATABASE_URL.get_secret_value().strip())

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
        async with self._sessions.begin() as session:
            yield session

    async def dispose(self):
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = self._sessions = None


database = Database(settings)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request dependency; services explicitly commit their short transactions."""
    database.engine
    async with database._sessions() as session:
        try:
            yield session
        finally:
            await session.rollback()
