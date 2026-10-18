import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app


@pytest.fixture
def session_factory():
    """A fresh, empty in-memory database for each test, so tests never touch support.db."""
    # An in-memory SQLite database lives only as long as its connection. StaticPool keeps a
    # single connection open, so every session in the test sees the same tables and rows.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def client(session_factory):
    """A test client whose requests use the test database instead of support.db."""

    def get_test_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    # Not opened with `with`, so the app's startup code, which creates support.db, never runs.
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
