"""Migrations build the schema the models describe, and every one of them can be undone."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.config import PROJECT_ROOT
from app.database import Base


@pytest.fixture
def scripts():
    return ScriptDirectory.from_config(Config(PROJECT_ROOT / "alembic.ini"))


def test_migrations_form_a_single_line(scripts):
    # Two heads means two migrations claim the same parent, usually from two branches.
    # Alembic then can't tell which comes first, and `upgrade head` refuses to run.
    assert len(scripts.get_heads()) == 1


def test_upgrade_builds_the_schema_the_models_describe(empty_database, migrate):
    migrate(empty_database, command.upgrade, "head")
    with empty_database.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    # Anything here means a model changed without a migration to match.
    assert differences == []


def test_downgrade_removes_every_table_and_type(empty_database, migrate):
    migrate(empty_database, command.upgrade, "head")
    migrate(empty_database, command.downgrade, "base")

    database = inspect(empty_database)
    assert database.get_table_names() == ["alembic_version"]
    assert database.get_enums() == []


def test_each_migration_can_be_undone_and_redone(empty_database, migrate, scripts):
    # Climb one migration at a time, stepping back down and up again at each one.
    oldest_first = reversed(list(scripts.walk_revisions()))
    for revision in [script.revision for script in oldest_first]:
        migrate(empty_database, command.upgrade, revision)
        migrate(empty_database, command.downgrade, "-1")
        migrate(empty_database, command.upgrade, revision)
