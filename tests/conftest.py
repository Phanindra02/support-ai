import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from app.main import app


@pytest.fixture(scope="session")
def test_database_url():
    """The Postgres database the tests may empty. It can never be the app's real database."""
    url = settings.test_database_url
    if url is None:
        pytest.fail("Set TEST_DATABASE_URL in .env. See .env.example.", pytrace=False)
    if not make_url(url).database.endswith("_test") or url == settings.database_url:
        pytest.fail("TEST_DATABASE_URL must name a database ending in _test.", pytrace=False)
    return url


@pytest.fixture
def engine():
    """A fresh, empty in-memory database for each test, so tests never touch support.db."""
    # An in-memory SQLite database lives only as long as its connection. StaticPool keeps a
    # single connection open, so every session in the test sees the same tables and rows.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    """Opens sessions on the test database."""
    return sessionmaker(bind=engine)


@pytest.fixture
def client(session_factory):
    """A test client whose requests use the test database instead of support.db."""

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
