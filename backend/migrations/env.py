"""Read URLs from settings without copying secrets into Alembic configuration."""

from alembic import context

from cinemind.config import Settings
from cinemind.database import make_engine, postgres_url
from cinemind.models import Base

config = context.config
settings = Settings()
secret = settings.migration_database_url or settings.database_url


def configure(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    if not secret:
        raise RuntimeError("Set DATABASE_URL or MIGRATION_DATABASE_URL")
    context.configure(url=postgres_url(secret.get_secret_value()), target_metadata=Base.metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    configure(config.attributes["connection"])
else:
    if not secret:
        raise RuntimeError("Set DATABASE_URL or MIGRATION_DATABASE_URL")
    engine = make_engine(secret.get_secret_value(), settings, migrations=True)
    try:
        with engine.connect() as connection:
            configure(connection)
    finally:
        engine.dispose()
