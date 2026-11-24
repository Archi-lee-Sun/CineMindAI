"""Lazy sync PostgreSQL engine; no startup connections or schema creation."""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from cinemind.config import Settings

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def postgres_url(raw: str):
    """Change only the driver scheme; preserve credentials and all URI parameters."""
    url = make_url(raw)
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError("A PostgreSQL URL using psycopg is required")
    return url.set(drivername="postgresql+psycopg")


def make_engine(raw: str, settings: Settings, *, migrations: bool = False) -> Engine:
    options = {"poolclass": NullPool} if migrations else {
        "pool_size": 5, "max_overflow": 5, "pool_timeout": 5, "pool_pre_ping": True,
    }
    engine = create_engine(
        postgres_url(raw), echo=False, hide_parameters=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds}, **options,
    )

    @event.listens_for(engine, "begin")
    def timeouts(connection):
        # Transaction-local settings also work with transaction-mode poolers.
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {settings.db_statement_timeout_ms}")
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = {settings.db_lock_timeout_ms}")

    return engine


class Database:
    def __init__(self, settings: Settings):
        self.engine = make_engine(settings.database_url.get_secret_value(), settings) if settings.database_url else None
        self.sessions = sessionmaker(self.engine, class_=Session, expire_on_commit=False)

    def require(self) -> None:
        if self.engine is None:
            from cinemind.errors import ServiceError
            raise ServiceError(503, "database_unavailable", "Database unavailable.")

    def close(self) -> None:
        if self.engine:
            self.engine.dispose()

    def transaction(self):
        self.require()
        return self.sessions.begin()
