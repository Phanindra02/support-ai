"""The bot: it answers a customer's question from the help docs, or says it doesn't know.

This is retrieval-augmented generation (RAG). The question is embedded with the same model as
the chunks (app/embeddings.py), pgvector finds the 4 chunks closest to it, and the LLM writes
an answer from those chunks alone. Answering only from the docs keeps it from making up a
refund policy, and saying "I don't know" lets a human take over instead.
"""

from dataclasses import dataclass

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import Chunk

# OpenAI's small, cheap model ($0.40 per million tokens in, $1.60 out). Rewording a few
# paragraphs it's given doesn't need a bigger one. About $0.0007 per question.
CHAT_MODEL = "gpt-4.1-mini"

# How many chunks go into the prompt. 4 chunks of about 250 tokens each is enough to hold an
# answer that spans two places in the docs, and keeps each question cheap.
CHUNK_COUNT = 4

# About 1,200 characters, under the 1,600 Twilio allows in one WhatsApp message.
MAX_ANSWER_TOKENS = 300

# What the bot says when the chunks don't hold the answer. Step 14 checks for it, to hand the
# question to a human.
DONT_KNOW = "I don't know."

INSTRUCTIONS = f"""\
You are a customer support assistant. Answer the customer's question using only the help doc \
excerpts you are given. Keep the answer short and friendly, in plain text.

If the excerpts don't contain the answer, reply with exactly "{DONT_KNOW}" and nothing else. \
Never guess, and never use anything you know that isn't in the excerpts."""


@dataclass
class BotAnswer:
    """What the bot replied, and the names of the help docs it answered from."""

    text: str
    sources: list[str]

    @property
    def known(self) -> bool:
        return not is_dont_know(self.text)


def is_dont_know(text: str) -> bool:
    """Whether the bot said it doesn't know, allowing for a curly apostrophe or no full stop."""
    return text.strip().replace("’", "'").rstrip(".!").lower() == "i don't know"


def get_chat_model() -> BaseChatModel:
    """OpenAI's chat model, through LangChain. Raises if OPENAI_API_KEY isn't set."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is empty, so the bot can't answer")
    return ChatOpenAI(
        model=CHAT_MODEL,
        # The same question gets the same answer, which also makes accuracy measurable.
        temperature=0,
        max_completion_tokens=MAX_ANSWER_TOKENS,
        api_key=settings.openai_api_key,
        # OpenAI's client waits up to 10 minutes. A customer won't.
        timeout=30,
    )


def find_chunks(db: Session, question: list[float], count: int = CHUNK_COUNT) -> list[Chunk]:
    """The chunks whose embeddings are closest to the question's, closest first.

    Cosine distance compares directions, not lengths, which is what OpenAI recommends for
    its embeddings. Each chunk's document comes in the same query, for its filename.
    """
    statement = (
        select(Chunk)
        .options(joinedload(Chunk.document))
        # Chunk.id breaks ties, so the same question always finds the same chunks.
        .order_by(Chunk.embedding.cosine_distance(question), Chunk.id)
        .limit(count)
    )
    return list(db.scalars(statement))


def build_prompt(question: str, chunks: list[Chunk]) -> list[BaseMessage]:
    """The instructions, then the excerpts with the doc each came from, then the question.

    The rules go in the system message and the customer's words in their own message, so a
    question like "ignore your instructions" is something to answer, not an order.
    """
    excerpts = "\n\n".join(
        f"[{number}] From {chunk.document.filename}:\n{chunk.text}"
        for number, chunk in enumerate(chunks, start=1)
    )
    return [
        SystemMessage(INSTRUCTIONS),
        HumanMessage(f"Help doc excerpts:\n\n{excerpts}\n\nCustomer's question: {question}"),
    ]


def answer_question(
    db: Session, embeddings: Embeddings, llm: BaseChatModel, question: str
) -> BotAnswer:
    """Find the chunks closest to the question, and have the LLM answer from them."""
    chunks = find_chunks(db, embeddings.embed_query(question))
    # End the read's transaction, so none stays open while OpenAI answers.
    db.commit()
    if not chunks:
        # No docs have been uploaded yet, so there's nothing to answer from or pay OpenAI for.
        return BotAnswer(text=DONT_KNOW, sources=[])
    reply = llm.invoke(build_prompt(question, chunks))
    text = reply.text.strip()
    if is_dont_know(text):
        # The chunks didn't hold the answer, so naming them as its source would mislead.
        return BotAnswer(text=DONT_KNOW, sources=[])
    # Each doc once, closest first. dict keeps the order things were added in.
    sources = list(dict.fromkeys(chunk.document.filename for chunk in chunks))
    return BotAnswer(text=text, sources=sources)
