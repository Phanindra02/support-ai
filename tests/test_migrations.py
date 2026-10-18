"""Migrations build the schema the models describe, and every one of them can be undone."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.config import PROJECT_ROOT
from app.database import Base


@pytest.fixture
def alembic_config():
    return Config(PROJECT_ROOT / "alembic.ini")


@pytest.fixture
def pg_engine(test_database_url):
    """The test database, emptied first, so each test starts from no tables at all."""
    engine = create_engine(test_database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def migrate(engine, config, step, revision):
    """Run an Alembic command, like command.upgrade, against the test database."""
    with engine.begin() as connection:
        # alembic/env.py uses this connection instead of the app's database.
        config.attributes["connection"] = connection
        step(config, revision)


def revisions_oldest_first(config):
    newest_first = ScriptDirectory.from_config(config).walk_revisions()
    return [script.revision for script in reversed(list(newest_first))]


def test_migrations_form_a_single_line(alembic_config):
    # Two heads means two migrations claim the same parent, usually from two branches.
    # Alembic then can't tell which comes first, and `upgrade head` refuses to run.
    assert len(ScriptDirectory.from_config(alembic_config).get_heads()) == 1


def test_upgrade_builds_the_schema_the_models_describe(pg_engine, alembic_config):
    migrate(pg_engine, alembic_config, command.upgrade, "head")
    with pg_engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    # Anything here means a model changed without a migration to match.
    assert differences == []


def test_downgrade_removes_every_table_and_type(pg_engine, alembic_config):
    migrate(pg_engine, alembic_config, command.upgrade, "head")
    migrate(pg_engine, alembic_config, command.downgrade, "base")

    database = inspect(pg_engine)
    assert database.get_table_names() == ["alembic_version"]
    assert database.get_enums() == []


def test_each_migration_can_be_undone_and_redone(pg_engine, alembic_config):
    # Climb one migration at a time, stepping back down and up again at each one.
    for revision in revisions_oldest_first(alembic_config):
        migrate(pg_engine, alembic_config, command.upgrade, revision)
        migrate(pg_engine, alembic_config, command.downgrade, "-1")
        migrate(pg_engine, alembic_config, command.upgrade, revision)
