"""POST /ask: the bot finds the closest chunks with pgvector and answers from them.

OpenAI is replaced by fakes (the use_bot fixture): embeddings that point every question the
same way, and LangChain's fake chat model, which replies with the text a test gives it.
"""

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.bot import (
    CHAT_MODEL,
    CHUNK_COUNT,
    DONT_KNOW,
    INSTRUCTIONS,
    MAX_ANSWER_TOKENS,
    OPENAI_TIMEOUT,
    get_bot,
    get_chat_model,
    is_dont_know,
)
from app.config import settings
from app.embeddings import EMBEDDING_MODEL

QUESTION = "How long does a refund take?"


def ask(client, question=QUESTION):
    return client.post("/ask", json={"question": question})


def test_the_bot_answers_from_the_four_closest_chunks(client, use_bot, save_chunks):
    # Questions point along x. Each chunk turns further away from it, and the last points the
    # other way entirely. The first is twice as long as the rest but no further away: cosine
    # distance compares directions.
    save_chunks(
        [
            ("refunds.pdf", "Refunds take 5 days.", (2.0, 0.2)),
            ("refunds.pdf", "Refunds go to the card you paid with.", (1.0, 0.3)),
            ("shipping.pdf", "Orders ship in 2 days.", (1.0, 0.5)),
            ("refunds.pdf", "Ask for a refund within 30 days.", (1.0, 0.8)),
            ("jobs.pdf", "We are hiring.", (0.0, 1.0)),
        ]
    )
    llm = use_bot(reply="Refunds take 5 days.")

    response = ask(client)

    assert response.status_code == 200
    # Each doc once, in the order its closest chunk came. jobs.pdf's chunk wasn't one of the 4.
    assert response.json() == {
        "answer": "Refunds take 5 days.",
        "sources": ["refunds.pdf", "shipping.pdf"],
    }
    [[system, human]] = llm.prompts
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


def test_when_the_docs_dont_hold_the_answer_it_says_so_and_names_no_source(
    client, use_bot, save_chunks
):
    save_chunks([("shipping.pdf", "Orders ship in 2 days.", (1.0, 0.0))])
    # A curly apostrophe and no full stop still count.
    use_bot(reply="I don’t know")

    response = ask(client)

    assert response.status_code == 200
    assert response.json() == {"answer": DONT_KNOW, "sources": []}


def test_with_no_docs_the_llm_isnt_asked(client, use_bot):
    llm = use_bot()

    response = ask(client)

    assert response.json() == {"answer": DONT_KNOW, "sources": []}
    assert llm.prompts == []


def test_the_question_is_trimmed_before_the_bot_sees_it(client, use_bot, save_chunks):
    save_chunks([("refunds.pdf", "Refunds take 5 days.", (1.0, 0.0))])
    llm = use_bot()

    ask(client, question=f"  {QUESTION}\n")

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
def test_a_bad_question_is_a_422(client, use_bot, body):
    use_bot()

    response = client.post("/ask", json=body)

    assert response.status_code == 422


def test_asking_needs_a_login(anonymous_client, use_bot):
    use_bot()

    response = ask(anonymous_client)

    assert response.status_code == 401


def test_without_an_openai_key_the_bot_isnt_set_up(client):
    # The autouse no_openai_key fixture has emptied the key, and the real bot is in place.
    response = ask(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "The bot isn't set up: OPENAI_API_KEY is empty"}


def test_when_openai_fails_its_a_503_not_a_500(client, use_bot, save_chunks, caplog):
    save_chunks([("refunds.pdf", "Refunds take 5 days.", (1.0, 0.0))])
    llm = use_bot(timed_out=True)

    response = ask(client)

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


def test_the_bot_uses_openais_small_models_and_gives_up_quickly(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-made-up-for-the-tests")
    bot = get_bot()
    assert bot.llm.model_name == CHAT_MODEL == "gpt-4.1-mini"
    assert bot.llm.temperature == 0
    assert bot.llm.max_tokens == MAX_ANSWER_TOKENS
    assert bot.llm.openai_api_key.get_secret_value() == "sk-made-up-for-the-tests"
    assert bot.embeddings.model == EMBEDDING_MODEL
    # Two calls of 6 seconds, with no retries, fit inside the 15 seconds Twilio waits.
    for model in (bot.llm, bot.embeddings):
        assert model.request_timeout == OPENAI_TIMEOUT == 6
        assert model.max_retries == 0


def test_without_a_key_there_is_no_bot():
    assert get_bot() is None
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is empty"):
        get_chat_model()
