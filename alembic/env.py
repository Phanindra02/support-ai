"""Alembic runs this file for every command. It connects to the database and runs migrations."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

import app.models  # noqa: F401  Importing the models adds their tables to Base.metadata.
from app.config import settings
from app.database import Base

config = context.config

# Set up logging from alembic.ini. disable_existing_loggers=False keeps loggers created
# before this point working, such as the app's own when the tests run migrations.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# What autogenerate compares the database against to write a new migration.
target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and run the migrations."""
    # The tests pass in a connection to the test database. Otherwise, use the app's database.
    connection = config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
        return

    # NullPool: a migration is a one-off command, so there's no point keeping connections open.
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        run_migrations(connection)


def run_migrations_offline() -> None:
    """Print the SQL instead of running it, for `alembic upgrade head --sql`."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
