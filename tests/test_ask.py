"""POST /ask: the bot finds the closest chunks with pgvector and answers from them.

OpenAI is replaced by fakes: embeddings that return the vector a test picks for each text, and
LangChain's fake chat model, which replies with the text a test gives it.
"""

import httpx
import openai
import pytest
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import Field

from app.bot import (
    CHAT_MODEL,
    CHUNK_COUNT,
    DONT_KNOW,
    INSTRUCTIONS,
    MAX_ANSWER_TOKENS,
    get_chat_model,
    is_dont_know,
)
from app.config import settings
from app.models import EMBEDDING_DIMENSIONS, Chunk, Document, DocumentStatus
from app.routers.ask import bot_chat_model, bot_embeddings

QUESTION = "How long does a refund take?"


def direction(x, y):
    """An embedding that points in the direction (x, y), padded with zeros."""
    return [x, y, *[0.0] * (EMBEDDING_DIMENSIONS - 2)]


class FakeEmbeddings(Embeddings):
    """Embeds every question as the same vector, so a test decides which chunks are closest."""

    def __init__(self, vector: list[float]):
        self.vector = vector

    def embed_documents(self, texts):
        raise AssertionError("Only questions are embedded here")

    def embed_query(self, text):
        return self.vector


class RecordingChatModel(FakeListChatModel):
    """LangChain's fake chat model, which also keeps every prompt it was sent."""

    prompts: list[list[BaseMessage]] = Field(default_factory=list)

    def _call(self, messages, *args, **kwargs):
        self.prompts.append(messages)
        return super()._call(messages, *args, **kwargs)


class TimedOutEmbeddings(FakeEmbeddings):
    """Fails like OpenAI does when it doesn't answer in time."""

    def embed_query(self, text):
        request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
        raise openai.APITimeoutError(request=request)


@pytest.fixture
def save_chunks(session_factory, make_agent):
    """Save chunks given as (filename, text, embedding), with a processed document for each
    filename, numbering each document's chunks in the order given."""
    agent = make_agent()

    def save(chunks):
        with session_factory() as db:
            documents: dict[str, Document] = {}
            positions: dict[str, int] = {}
            for filename, text, embedding in chunks:
                if filename not in documents:
                    documents[filename] = Document(
                        filename=filename,
                        s3_key=f"uploads/{filename}",
                        size_bytes=641,
                        status=DocumentStatus.processed,
                        uploaded_by_id=agent.id,
                    )
                    positions[filename] = 0
                document = documents[filename]
                position = positions[filename]
                positions[filename] += 1
                db.add(Chunk(document=document, position=position, text=text, embedding=embedding))
            db.commit()

    return save


@pytest.fixture
def ask(test_app, client):
    """Ask the bot as a logged-in agent, with the fakes in place of OpenAI."""

    def ask(question=QUESTION, reply="Refunds take 5 days.", embeddings=None):
        llm = RecordingChatModel(responses=[reply])
        test_app.dependency_overrides[bot_embeddings] = lambda: (
            embeddings or FakeEmbeddings(direction(1.0, 0.0))
        )
        test_app.dependency_overrides[bot_chat_model] = lambda: llm
        return client.post("/ask", json={"question": question}), llm

    return ask


def test_the_bot_answers_from_the_four_closest_chunks(save_chunks, ask):
    # The question points along x. Each chunk turns further away from it, and the last points
    # the other way entirely. A chunk twice as long but pointing the same way is just as close:
    # cosine distance compares directions.
    save_chunks(
        [
            ("refunds.pdf", "Refunds take 5 days.", direction(2.0, 0.2)),
            ("refunds.pdf", "Refunds go to the card you paid with.", direction(1.0, 0.3)),
            ("shipping.pdf", "Orders ship in 2 days.", direction(1.0, 0.5)),
            ("refunds.pdf", "Ask for a refund within 30 days.", direction(1.0, 0.8)),
            ("jobs.pdf", "We are hiring.", direction(0.0, 1.0)),
        ]
    )

    response, llm = ask()

    assert response.status_code == 200
    # Each doc once, in the order its closest chunk came. jobs.pdf's chunk wasn't one of the 4.
    assert response.json() == {
        "answer": "Refunds take 5 days.",
        "sources": ["refunds.pdf", "shipping.pdf"],
    }
    [prompt] = llm.prompts
    system, human = prompt
    assert isinstance(system, SystemMessage) and system.content == INSTRUCTIONS
    assert isinstance(human, HumanMessage)
    assert human.content == (
        "Help doc excerpts:\n\n"
        "[1] From refunds.pdf:\nRefunds take 5 days.\n\n"
        "[2] From refunds.pdf:\nRefunds go to the card you paid with.\n\n"
        "[3] From shipping.pdf:\nOrders ship in 2 days.\n\n"
        "[4] From refunds.pdf:\nAsk for a refund within 30 days.\n\n"
        f"Customer's question: {QUESTION}"
    )
    assert CHUNK_COUNT == 4


def test_when_the_docs_dont_hold_the_answer_it_says_so_and_names_no_source(save_chunks, ask):
    save_chunks([("shipping.pdf", "Orders ship in 2 days.", direction(1.0, 0.0))])

    # A curly apostrophe and no full stop still count.
    response, _ = ask(reply="I don’t know")

    assert response.status_code == 200
    assert response.json() == {"answer": DONT_KNOW, "sources": []}


def test_with_no_docs_the_llm_isnt_asked(ask):
    response, llm = ask()

    assert response.json() == {"answer": DONT_KNOW, "sources": []}
    assert llm.prompts == []


def test_the_question_is_trimmed_before_the_bot_sees_it(save_chunks, ask):
    save_chunks([("refunds.pdf", "Refunds take 5 days.", direction(1.0, 0.0))])

    _, llm = ask(question=f"  {QUESTION}\n")

    assert llm.prompts[0][1].content.endswith(f"Customer's question: {QUESTION}")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": "a" * 1601},
        {"question": "Hi\x00"},
        {"question": QUESTION, "language": "en"},
    ],
)
def test_a_bad_question_is_a_422(ask, client, body):
    ask()  # Puts the fakes in place.

    response = client.post("/ask", json=body)

    assert response.status_code == 422


def test_asking_needs_a_login(anonymous_client):
    response = anonymous_client.post("/ask", json={"question": QUESTION})

    assert response.status_code == 401


def test_without_an_openai_key_the_bot_isnt_set_up(client):
    # The autouse no_openai_key fixture has emptied the key, and the real models are in place.
    response = client.post("/ask", json={"question": QUESTION})

    assert response.status_code == 503
    assert response.json() == {"detail": "The bot isn't set up: OPENAI_API_KEY is empty"}


def test_when_openai_fails_its_a_503_not_a_500(save_chunks, ask, caplog):
    save_chunks([("refunds.pdf", "Refunds take 5 days.", direction(1.0, 0.0))])

    response, llm = ask(embeddings=TimedOutEmbeddings(direction(1.0, 0.0)))

    assert response.status_code == 503
    assert response.json() == {"detail": "The bot can't answer right now. Try again."}
    assert "OpenAI couldn't answer a question" in caplog.text
    assert llm.prompts == []


@pytest.mark.parametrize(
    "text", ["I don't know.", "I don't know", "  i don't know!\n", "I don’t know."]
)
def test_these_mean_the_bot_doesnt_know(text):
    assert is_dont_know(text)


@pytest.mark.parametrize(
    "text", ["Refunds take 5 days.", "I don't know your order number, but refunds take 5 days."]
)
def test_these_are_answers(text):
    assert not is_dont_know(text)


def test_the_chat_model_is_openais_small_one_at_temperature_0(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-made-up-for-the-tests")
    llm = get_chat_model()
    assert llm.model_name == CHAT_MODEL == "gpt-4.1-mini"
    assert llm.temperature == 0
    assert llm.max_tokens == MAX_ANSWER_TOKENS
    assert llm.openai_api_key.get_secret_value() == "sk-made-up-for-the-tests"
    assert llm.request_timeout == 30


def test_without_a_key_there_is_no_chat_model():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is empty"):
        get_chat_model()
