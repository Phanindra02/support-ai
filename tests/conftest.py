"""Shared test fixtures. Tests run on the Postgres database named in TEST_DATABASE_URL."""

import boto3
import httpx
import pytest
from alembic import command
from alembic.config import Config
from botocore.config import Config as BotoConfig
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage
from moto import mock_aws
from openai import APITimeoutError
from pydantic import Field
from redis import Redis
from sqlalchemy import make_url, text
from sqlalchemy.orm import sessionmaker

from app.alerts import get_http_client
from app.bot import Bot, get_bot
from app.cache import get_redis, make_redis
from app.config import PROJECT_ROOT, settings
from app.database import Base, get_db, make_engine
from app.main import app
from app.models import EMBEDDING_DIMENSIONS, Agent, AgentRole, Chunk, Document, DocumentStatus
from app.queue import get_queue, make_queue
from app.security import create_access_token, hash_password
from app.storage import get_s3_client


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


def redis_location(url):
    """The host, port, and database number a Redis URL points at."""
    options = Redis.from_url(url).connection_pool.connection_kwargs
    return options.get("host"), options.get("port"), options.get("db", 0)


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
def test_redis_url():
    """The Redis database the tests may empty. It can never be the app's."""
    url = settings.test_redis_url
    if url is None:
        pytest.fail("Set TEST_REDIS_URL in .env. See .env.example.", pytrace=False)
    if redis_location(url) == redis_location(settings.redis_url):
        pytest.fail("TEST_REDIS_URL must differ from REDIS_URL.", pytrace=False)
    return url


@pytest.fixture
def redis_client(test_redis_url):
    """The test Redis database, emptied before and after each test."""
    client = make_redis(test_redis_url)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture
def test_queue(test_redis_url, redis_client):
    """The job queue, on the tests' Redis database, which redis_client empties before and after."""
    return make_queue(test_redis_url)


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


class FakeN8n:
    """Stands in for n8n, so no test calls the real one. Keeps every request it gets."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        # Replace to answer differently, or raise an httpx error to act as if n8n were down.
        self.reply = lambda request: httpx.Response(200, json={"message": "Workflow was started"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)


@pytest.fixture
def n8n():
    return FakeN8n()


def unreachable_s3():
    """An S3 client with made-up keys, pointed at port 1, where nothing listens.

    Every test gets this one unless it asks for the s3 fixture, so no test can ever upload to
    the real bucket, even with real AWS keys in ~/.aws.
    """
    return boto3.client(
        "s3",
        region_name="us-east-2",
        endpoint_url="http://localhost:1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=BotoConfig(retries={"total_max_attempts": 1}),
    )


@pytest.fixture(autouse=True)
def no_openai_key(monkeypatch):
    """Empty the OpenAI key for every test, so none can call the real OpenAI and spend money,
    even with a real key in .env. Tests that need embeddings pass in LangChain's fake ones."""
    monkeypatch.setattr(settings, "openai_api_key", None)


@pytest.fixture(scope="session")
def test_bucket():
    """The bucket the app uploads to in the tests, in the fake S3 the s3 fixture starts."""
    return "support-ai-docs-test"


@pytest.fixture
def test_app(session_factory, redis_client, test_queue, n8n, test_bucket, monkeypatch):
    """The app, using the test database, Redis, and fakes for n8n and S3 instead of its own."""

    def get_test_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_redis] = lambda: redis_client
    app.dependency_overrides[get_queue] = lambda: test_queue
    fake_n8n_client = httpx.Client(transport=httpx.MockTransport(n8n.handle))
    app.dependency_overrides[get_http_client] = lambda: fake_n8n_client
    # Made only when a request asks for it, since most tests never touch S3.
    app.dependency_overrides[get_s3_client] = unreachable_s3
    monkeypatch.setattr(settings, "s3_bucket", test_bucket)
    yield app
    app.dependency_overrides.clear()
    fake_n8n_client.close()


@pytest.fixture
def anonymous_client(test_app):
    """A test client that isn't logged in."""
    return TestClient(test_app)


@pytest.fixture
def client(test_app, make_agent, auth_header):
    """A test client logged in as a plain agent, which is all the ticket endpoints need."""
    agent = make_agent(name="Ada Lovelace", email="ada@example.com")
    return TestClient(test_app, headers=auth_header(agent))


@pytest.fixture
def admin_client(test_app, make_agent, auth_header):
    """A test client logged in as an admin."""
    admin = make_agent(name="Grace Hopper", email="grace@example.com", role=AgentRole.admin)
    return TestClient(test_app, headers=auth_header(admin))


@pytest.fixture
def s3(test_app, test_bucket):
    """A fake S3 that keeps objects in memory (moto), holding an empty test bucket."""
    # mock_aws catches every boto3 call to AWS, and swaps in made-up keys while it runs.
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-2")
        client.create_bucket(
            Bucket=test_bucket, CreateBucketConfiguration={"LocationConstraint": "us-east-2"}
        )
        test_app.dependency_overrides[get_s3_client] = lambda: client
        yield client


@pytest.fixture
def redis_is_down(test_app):
    """Point the app at a Redis that isn't running. Nothing listens on port 1."""
    stopped = make_redis("redis://localhost:1/0")
    app.dependency_overrides[get_redis] = lambda: stopped
    yield
    stopped.close()


@pytest.fixture(scope="session")
def agent_password():
    """The password every agent made by make_agent has."""
    return "correct horse battery staple"


@pytest.fixture(scope="session")
def agent_password_hash(agent_password):
    """Hashing is slow on purpose (about 30 ms), so the tests' password is hashed once."""
    return hash_password(agent_password)


@pytest.fixture
def make_agent(session_factory, agent_password_hash):
    """Save an agent who can log in with agent_password. Keyword arguments change fields."""

    def make(**fields):
        with session_factory() as db:
            agent = Agent(
                **{
                    "name": "Sam Lee",
                    "email": "sam.lee@example.com",
                    "password_hash": agent_password_hash,
                    **fields,
                }
            )
            db.add(agent)
            db.commit()
            db.refresh(agent)  # Loads the id and defaults while the session is still open.
            return agent

    return make


@pytest.fixture
def auth_header():
    """The header that logs a request in as an agent, without going through /auth/token."""

    def header(agent):
        return {"Authorization": f"Bearer {create_access_token(agent)}"}

    return header


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


# The bot, with fakes for OpenAI


def direction(x, y):
    """An embedding that points in the direction (x, y), padded with zeros."""
    return [x, y, *[0.0] * (EMBEDDING_DIMENSIONS - 2)]


class FakeEmbeddings(Embeddings):
    """Embeds every question as the direction (1, 0), so a test decides which chunks are
    closest by where it points them. With timed_out, fails like OpenAI does when it's slow."""

    def __init__(self, timed_out=False):
        self.timed_out = timed_out

    def embed_documents(self, texts):
        raise AssertionError("The bot only embeds questions")

    def embed_query(self, text):
        if self.timed_out:
            request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
            raise APITimeoutError(request=request)
        return direction(1.0, 0.0)


class RecordingChatModel(FakeListChatModel):
    """LangChain's fake chat model, which also keeps every prompt it was sent."""

    prompts: list[list[BaseMessage]] = Field(default_factory=list)

    def _call(self, messages, *args, **kwargs):
        self.prompts.append(messages)
        return super()._call(messages, *args, **kwargs)


@pytest.fixture
def use_bot(test_app):
    """Give the app a bot whose LLM replies with the given text. Returns that LLM, which
    keeps the prompts it was sent. Without this, the bot isn't set up, as with no key."""

    def use(reply="Refunds take 5 days.", timed_out=False):
        llm = RecordingChatModel(responses=[reply])
        bot = Bot(embeddings=FakeEmbeddings(timed_out), llm=llm)
        test_app.dependency_overrides[get_bot] = lambda: bot
        return llm

    return use


@pytest.fixture
def save_chunks(session_factory, make_agent):
    """Save chunks given as (filename, text, (x, y)), each embedded as that direction, with a
    processed document for each filename."""
    agent = make_agent(name="Docs Owner", email="docs.owner@example.com")

    def save(chunks):
        with session_factory() as db:
            documents: dict[str, Document] = {}
            # Numbered across all the documents, which keeps them unique within each one too.
            for position, (filename, text, (x, y)) in enumerate(chunks):
                if filename not in documents:
                    documents[filename] = Document(
                        filename=filename,
                        s3_key=f"uploads/{filename}",
                        size_bytes=641,
                        status=DocumentStatus.processed,
                        uploaded_by_id=agent.id,
                    )
                db.add(
                    Chunk(
                        document=documents[filename],
                        position=position,
                        text=text,
                        embedding=direction(x, y),
                    )
                )
            db.commit()

    return save
