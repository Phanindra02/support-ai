"""The jobs the worker runs. The API queues each one by its import path (app/queue.py)."""

import logging

import httpx
from botocore.client import BaseClient
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.alerts import get_http_client, send_urgent_alert
from app.bot import start_new_docs_version
from app.cache import clear_cache, get_redis
from app.config import settings
from app.database import SessionLocal
from app.embeddings import get_embeddings
from app.models import Chunk, Document, Message, Ticket, TicketPriority
from app.routers.tickets import STATS_KEY
from app.schemas import UrgentAlert
from app.storage import get_s3_client, read_text

logger = logging.getLogger(__name__)

# About 1,000 characters is a few paragraphs: enough to hold a whole answer, and small enough
# that the 4 closest chunks fit easily in the prompt. Each chunk repeats the last 200 or so
# characters of the one before, so a sentence cut at a boundary is whole in one of them.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def split_text(
    text: str, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Cut text into chunks of at most chunk_size characters.

    The splitter cuts between paragraphs where it can, then between lines, then between
    words, and inside a word only when one word is longer than chunk_size.
    """
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_text(text)


def chunk_document(db: Session, s3: BaseClient, document_id: int) -> list[str]:
    """Read a processed document's text from S3 and split it into chunks."""
    document = db.get(Document, document_id)
    if document is None:
        raise LookupError(f"No document has id {document_id}")
    if document.text_s3_key is None:
        raise ValueError(f"Document {document_id} isn't processed yet: its text isn't in S3")
    if not settings.s3_bucket:
        raise RuntimeError("S3_BUCKET is empty, so the document's text can't be read")
    text = read_text(s3, settings.s3_bucket, document.text_s3_key)
    return split_text(text)


def save_chunks(db: Session, s3: BaseClient, embeddings: Embeddings, document_id: int) -> int:
    """Split a processed document's text, embed each chunk, and replace its chunks with these.

    Replacing, rather than adding, means a second run for the same document leaves one copy
    of each chunk, and a document whose text changed loses its old ones. Returns the count.
    """
    texts = chunk_document(db, s3, document_id)
    # End the read's transaction, so none stays open while OpenAI answers.
    db.commit()
    # One call for all of the document's chunks (LangChain sends up to 1,000 per request)
    # rather than one call each. It comes before the lock below, so the lock isn't held while
    # OpenAI answers. If OpenAI fails, nothing has changed, and the document keeps its chunks.
    vectors = embeddings.embed_documents(texts) if texts else []
    # Lock the document's row until the commit, so two runs for one document take turns here
    # instead of both saving chunks at the same positions.
    db.execute(select(Document.id).where(Document.id == document_id).with_for_update())
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.add_all(
        Chunk(document_id=document_id, position=position, text=text, embedding=vector)
        for position, (text, vector) in enumerate(zip(texts, vectors, strict=True))
    )
    db.commit()
    return len(texts)


def embed_document(document_id: int) -> int:
    """The job the API queues when the Lambda marks a document processed.

    It raises if something goes wrong, so RQ marks the job failed and keeps it with its error.
    Returns how many chunks the document has now.
    """
    with SessionLocal() as db:
        count = save_chunks(db, get_s3_client(), get_embeddings(), document_id)
    # After the commit, so an answer cached from here on comes from the new chunks.
    start_new_docs_version(get_redis())
    if count:
        logger.info("Document %s has %s chunks, each with its embedding", document_id, count)
    else:
        # Most likely a scanned PDF, whose pages are pictures of text.
        logger.warning("Document %s has no text, so the bot can't answer from it", document_id)
    return count


# The model service answers in milliseconds. Waiting longer than this means it's down or
# stuck, and the job should fail so RQ tries again later.
MODEL_SERVICE_TIMEOUT_SECONDS = 5
# The longest text the model service takes (ml/service.py).
MAX_PREDICT_CHARS = 10_000
# A customer is very negative when the sentiment model scores "negative" at least this high.
# On the test tweets, 99.1% of messages at 0.8 or more were negative, and they were a fifth of
# all messages, so the team hears about the angriest customers, not every complaint
# (docs/results.md).
VERY_NEGATIVE = 0.8


def first_message(db: Session, ticket: Ticket) -> str:
    first = db.scalar(
        select(Message)
        .where(Message.ticket_id == ticket.id)
        .order_by(Message.created_at, Message.id)
        .limit(1)
    )
    return first.body if first is not None else ""


def ask_model(client: httpx.Client, name: str, text: str) -> dict:
    """The model service's prediction: the label and each label's score."""
    response = client.post(
        f"{settings.model_service_url}/predict/{name}", json={"text": text[:MAX_PREDICT_CHARS]}
    )
    response.raise_for_status()
    return response.json()


def alert_reason(priority: dict, sentiment: dict | None) -> str | None:
    """Why the team should hear about the ticket now, for the Slack message, or None."""
    reasons = []
    if priority["label"] == TicketPriority.high:
        reasons.append(f"high priority ({priority['scores']['high']:.0%} sure)")
    negative = sentiment["scores"].get("negative", 0) if sentiment else 0
    if negative >= VERY_NEGATIVE:
        reasons.append(f"a very negative customer ({negative:.0%} sure)")
    return "the model: " + " and ".join(reasons) if reasons else None


def triage(
    db: Session, models: httpx.Client, alerts: httpx.Client, ticket_id: int
) -> TicketPriority | None:
    """Predict the ticket's priority and the customer's mood, save the priority, and alert.

    The job runs seconds after the ticket opens, before an agent could have set a priority.
    Both predictions come before anything is saved, so if either call fails, nothing has
    changed and no alert has gone out when RQ tries again.
    """
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        logger.warning("Ticket %s doesn't exist, so there's nothing to triage", ticket_id)
        return None
    message = first_message(db, ticket)
    # The subject, a blank line, then the first message: how the training tickets read.
    priority = ask_model(models, "priority", f"{ticket.subject}\n\n{message}".strip())
    # The mood is in the customer's own words, not the subject.
    sentiment = ask_model(models, "sentiment", message) if message.strip() else None
    ticket.priority = TicketPriority(priority["label"])
    db.commit()

    reason = alert_reason(priority, sentiment)
    if reason is not None:
        alert = UrgentAlert(
            ticket_id=ticket.id,
            subject=ticket.subject,
            customer_name=ticket.customer.name,
            status=ticket.status,
            priority=ticket.priority,
            created_at=ticket.created_at,
            # The Slack message says "Marked urgent by ...", so the n8n workflow stays as it is.
            marked_urgent_by=reason,
        )
        # Never raises: a failed alert is logged, and the saved priority stays.
        send_urgent_alert(alerts, alert)
    return ticket.priority


def triage_ticket(ticket_id: int) -> str | None:
    """The job the API queues for each new ticket. Returns the priority it set."""
    with SessionLocal() as db, httpx.Client(timeout=MODEL_SERVICE_TIMEOUT_SECONDS) as models:
        priority = triage(db, models, get_http_client(), ticket_id)
    if priority is not None:
        # The backlog counts by priority just changed.
        clear_cache(get_redis(), STATS_KEY)
        logger.info("Ticket %s is %s priority, as the model predicted", ticket_id, priority)
    return priority
