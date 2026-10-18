"""The tests run on their own Postgres database, which starts empty for every test."""

from datetime import timedelta

from sqlalchemy import func, make_url, select, text

from app.config import settings
from app.models import Ticket


def test_tests_use_a_separate_postgres_database(engine):
    assert engine.dialect.name == "postgresql"
    assert engine.url.database.endswith("_test")
    assert engine.url.database != make_url(settings.database_url).database


def test_each_test_starts_with_empty_tables_and_ids_from_1(session_factory, make_ticket):
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Ticket)) == 0
    assert make_ticket()["id"] == 1


def test_postgres_returns_times_in_utc(engine):
    with engine.connect() as connection:
        now = connection.scalar(text("SELECT now()"))
    assert now.utcoffset() == timedelta(0)
