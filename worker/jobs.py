"""The jobs the worker runs. The API queues each one by its import path (app/queue.py)."""

import logging

from botocore.client import BaseClient
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Document
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


def embed_document(document_id: int) -> int:
    """The job the API queues when the Lambda marks a document processed.

    It raises if something goes wrong, so RQ marks the job failed and keeps it with its error.
    Returns how many chunks the document's text made.
    """
    with SessionLocal() as db:
        chunks = chunk_document(db, get_s3_client(), document_id)
    logger.info("Document %s split into %s chunks", document_id, len(chunks))
    return len(chunks)
