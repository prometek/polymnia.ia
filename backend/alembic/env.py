"""Alembic environment — wired to the app's engine and SQLModel metadata.

The migration target of truth is the same `SQLModel.metadata` the app builds its
schema from (api/models.py). We reuse `api.session` so the connection string
(DATABASE_URL, normalised to the psycopg 3 driver) is defined in exactly one place.
"""

from logging.config import fileConfig

from alembic import context

# Importing api.session builds the engine and, via `from . import models`,
# registers every table on SQLModel.metadata.
from api.session import DATABASE_URL, engine
from sqlmodel import SQLModel

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (`alembic upgrade --sql`)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the live database using the app's engine."""
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
