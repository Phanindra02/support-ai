"""Shared test fixtures. Tests run on the Postgres database named in TEST_DATABASE_URL."""

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text
from sqlalchemy.orm import sessionmaker

from app.config import PROJECT_ROOT, settings
from app.database import Base, get_db, make_engine
from app.main import app


def run_alembic(engine, step, revision):
    """Run an Alembic command, such as command.upgrade, on an engine's database."""
    config = Config(PROJECT_ROOT / "alembic.ini")
    with engine.begin() as connection:
        # alembic/env.py uses this connection instead of the app's database.
        config.attributes["connection"] = connection
        step(config, revision)


def drop_everything(engine):
    """Remove every table and type, leaving an empty database."""
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture(scope="session")
def test_database_url():
    """The Postgres database the tests may empty. It can never be the app's real database."""
    url = settings.test_database_url
    if url is None:
        pytest.fail("Set TEST_DATABASE_URL in .env. See .env.example.", pytrace=False)
    if not make_url(url).database.endswith("_test") or url == settings.database_url:
        pytest.fail("TEST_DATABASE_URL must name a database ending in _test.", pytrace=False)
    return url


@pytest.fixture(scope="session")
def test_database(test_database_url):
    """The test database, built by running every migration once per test run."""
    # The same engine settings as the app's, so times come back in UTC here too.
    engine = make_engine(test_database_url)
    drop_everything(engine)
    run_alembic(engine, command.upgrade, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def engine(test_database):
    """The test database. Every table is emptied after each test, so tests can't affect others."""
    yield test_database
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with test_database.begin() as connection:
        # Fail instead of hanging if a test left a transaction open on one of the tables.
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        # RESTART IDENTITY starts ids from 1 again.
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY"))


@pytest.fixture
def empty_database(test_database):
    """The test database with no tables at all. It's rebuilt afterwards for the other tests."""
    drop_everything(test_database)
    yield test_database
    drop_everything(test_database)
    run_alembic(test_database, command.upgrade, "head")


@pytest.fixture
def migrate():
    """Runs Alembic commands on a test database: migrate(engine, command.upgrade, "head")."""
    return run_alembic


@pytest.fixture
def session_factory(engine):
    """Opens sessions on the test database."""
    return sessionmaker(bind=engine)


@pytest.fixture
def client(session_factory):
    """A test client whose requests use the test database instead of the app's."""

    def get_test_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def ticket_data():
    """A valid body for POST /tickets. Tests copy it and change one field."""
    return {
        "customer_name": "Jane Doe",
        "phone": "+16145550123",
        "subject": "Order late",
        "message": "Where is my order?",
    }


@pytest.fixture
def make_ticket(client, ticket_data):
    """Create a ticket through the API and return its JSON. Keyword arguments change fields."""

    def make(**fields):
        response = client.post("/tickets", json={**ticket_data, **fields})
        assert response.status_code == 201, response.text
        return response.json()

    return make
