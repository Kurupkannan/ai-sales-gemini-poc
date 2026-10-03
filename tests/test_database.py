"""Database configuration tests require no network or credentials."""

import pytest
from app.config import Settings
from app.db.session import Database, DatabaseConfigurationError, connection_url


def config(**values):
    return Settings(
        _env_file=None, DATABASE_URL="", MIGRATION_DATABASE_URL="", **values
    )


def test_urls_and_secrets():
    settings = config()
    url = connection_url(
        "postgresql://user:p%40ss@db.example.com:5432/postgres?sslmode=disable",
        settings,
    )
    assert url.password == "p@ss"
    assert url.drivername == "postgresql+psycopg"
    assert url.query["sslmode"] == "verify-full"
    assert url.query["sslrootcert"] == "system"
    settings = Settings(
        _env_file=None, DATABASE_URL="postgresql://user:secret@localhost/postgres"
    )
    assert "secret" not in repr(settings)


@pytest.mark.parametrize(
    "url",
    ["", "nonsense", "sqlite:///db.sqlite", "postgresql://user@host:6543/postgres"],
)
def test_invalid_connections(url):
    with pytest.raises(DatabaseConfigurationError):
        connection_url(url, config())


def test_tls_disable_only_for_local_postgres():
    settings = config(DB_SSLMODE="disable")
    assert (
        connection_url("postgresql://user@127.0.0.1/db", settings).query["sslmode"]
        == "disable"
    )
    with pytest.raises(DatabaseConfigurationError):
        connection_url("postgresql://user@db.example.com/db", settings)


async def test_database_is_lazy_and_optional():
    db = Database(config())
    assert not db.configured
    assert db._engine is None
    await db.dispose()
    with pytest.raises(DatabaseConfigurationError):
        _ = db.engine


@pytest.mark.parametrize("migration", [False, True])
async def test_separate_password_for_runtime_and_migrations(migration):
    from app.db.session import build_engine

    password = "p@ss:/?#% with spaces"
    settings = config(DATABASE_PASSWORD=password)
    engine = build_engine(
        "postgresql://postgres.project@db.example.com:5432/postgres",
        settings,
        migration=migration,
    )
    try:
        assert engine.url.password == password
        assert engine.url.host == "db.example.com"
        assert engine.url.username == "postgres.project"
        assert password not in repr(settings)
        assert password not in str(engine.url)
    finally:
        await engine.dispose()


def test_embedded_migration_password_takes_precedence():
    url = connection_url(
        "postgresql://migrator:other%40password@db.example.com/postgres",
        config(DATABASE_PASSWORD="shared-password"),
    )
    assert url.password == "other@password"
