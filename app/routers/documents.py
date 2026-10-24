"""Help docs: agents upload PDFs here, and in Stage 6 the WhatsApp bot answers from them.

POST /documents saves the PDF to S3 and records it as "uploaded". S3 then runs the Lambda in
lambda/, which pulls out the text, saves it to S3 too, and calls
POST /webhooks/document-processed to mark the document "processed". That queues a job for the
worker, which splits the text into chunks and embeds them.
"""

import hmac
import logging
import os
from typing import Annotated

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from redis import RedisError
from sqlalchemy import select

from app.config import settings
from app.database import SessionDep
from app.models import Document, DocumentStatus
from app.queue import EMBED_DOCUMENT, EMBED_RETRY, QueueDep
from app.schemas import DocumentProcessed, DocumentRead, ErrorResponse
from app.security import CurrentAgent, get_current_agent
from app.storage import get_s3_client, new_upload_key, save_pdf

logger = logging.getLogger(__name__)

# Far bigger than a help doc needs to be. The Lambda reads the whole PDF into memory.
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MiB

# Every PDF starts with these bytes. The file's name and the Content-Type the client sends
# can claim anything, so the content itself is checked.
PDF_SIGNATURE = b"%PDF-"

# The size of the filename column.
MAX_FILENAME_LENGTH = 255

# The header the Lambda sends the shared secret in, the same one the API sends n8n's in.
SECRET_HEADER = "X-Webhook-Secret"

S3Dep = Annotated[BaseClient, Depends(get_s3_client)]

# Uploading needs a logged-in agent, so no one else can feed the bot what to say.
router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(get_current_agent)],
    responses={401: {"model": ErrorResponse, "description": "Not logged in"}},
)


def invalid_file(message: str) -> RequestValidationError:
    """A 422 in FastAPI's standard shape, the same as for any other bad input."""
    error = {"type": "value_error", "loc": ("body", "file"), "msg": message, "input": None}
    return RequestValidationError([error])


def file_size(file: UploadFile) -> int:
    """How many bytes were uploaded. The whole file has arrived before the endpoint runs."""
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    file.file.seek(0)
    return size


@router.post(
    "",
    response_model=DocumentRead,
    status_code=201,
    responses={
        413: {"model": ErrorResponse, "description": "The file is over 10 MiB"},
        503: {"model": ErrorResponse, "description": "S3 isn't set up, or can't be reached"},
    },
)
def upload_document(file: UploadFile, db: SessionDep, agent: CurrentAgent, s3: S3Dep):
    """Upload a help doc: a PDF of up to 10 MiB, sent as the multipart form field `file`."""
    bucket = settings.s3_bucket
    if not bucket:
        raise HTTPException(status_code=503, detail="Uploads aren't set up: S3_BUCKET is empty")

    # Trimmed and checked like any other text field. Postgres can't store NUL.
    filename = (file.filename or "").strip()
    if not filename:
        raise invalid_file("The file needs a name")
    if len(filename) > MAX_FILENAME_LENGTH:
        raise invalid_file(f"The file's name can be at most {MAX_FILENAME_LENGTH} characters")
    if "\x00" in filename:
        raise invalid_file("The file's name can't contain the NUL character (\\u0000)")

    # The app's body limit (app/body_limit.py) has already stopped anything much bigger as it
    # arrived. This catches a file just over 10 MiB, which fits in the form's spare room.
    size = file_size(file)
    if size > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="The file is over 10 MiB")
    if file.file.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
        raise invalid_file("The file isn't a PDF")
    file.file.seek(0)

    key = new_upload_key()
    try:
        save_pdf(s3, bucket, key, file.file)
    except (BotoCoreError, ClientError) as error:
        # Such as no AWS credentials, a missing bucket, or S3 being unreachable. None of
        # those is the agent's fault, so it's a 503 to try again later, not a 500.
        logger.warning("Couldn't save an upload to S3: %s", error)
        raise HTTPException(status_code=503, detail="Couldn't save the file to S3. Try again.")

    # S3 first, then the row, so a row never points to a file that isn't there. If saving the
    # row fails, the file is left in S3 with nothing pointing to it, which costs next to nothing.
    document = Document(filename=filename, s3_key=key, size_bytes=size, uploaded_by_id=agent.id)
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def verify_lambda_secret(secret: Annotated[str | None, Header(alias=SECRET_HEADER)] = None) -> None:
    """Stop any request that doesn't carry the shared secret, with a 403."""
    if not settings.lambda_webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="Document processing isn't set up: LAMBDA_WEBHOOK_SECRET is empty",
        )
    # compare_digest takes as long when the first character is wrong as when the last one is,
    # so timing the answers can't reveal the secret a character at a time. It's given bytes,
    # because with text it only accepts plain ASCII, and anything else would crash with a 500.
    given = (secret or "").encode()
    if not hmac.compare_digest(given, settings.lambda_webhook_secret.encode()):
        raise HTTPException(status_code=403, detail="Wrong or missing X-Webhook-Secret")


# The Lambda's call, on its own router: it has the shared secret rather than an agent's login.
# Like Twilio's webhook, it skips the per-IP rate limit (app/main.py), since every call comes
# from AWS's addresses, and the secret is checked before anything else.
webhook_router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
    dependencies=[Depends(verify_lambda_secret)],
    responses={
        403: {"model": ErrorResponse, "description": "Wrong or missing X-Webhook-Secret"},
        503: {"model": ErrorResponse, "description": "LAMBDA_WEBHOOK_SECRET isn't set"},
    },
)


@webhook_router.post(
    "/document-processed",
    response_model=DocumentRead,
    responses={
        404: {"model": ErrorResponse, "description": "No document has that s3_key"},
        503: {
            "model": ErrorResponse,
            "description": "LAMBDA_WEBHOOK_SECRET isn't set, or the job queue can't be reached",
        },
    },
)
def mark_document_processed(update: DocumentProcessed, db: SessionDep, queue: QueueDep):
    """The Lambda calls this once it has saved a PDF's text to S3. It queues the job that
    splits the text into chunks and embeds them for the bot.

    The Lambda can safely call it again for the same document, and S3 can deliver the same
    upload's event twice. Each call queues the job again, and the job replaces the document's
    chunks, so the bot ends up with one copy of each.
    """
    document = db.scalar(select(Document).where(Document.s3_key == update.s3_key))
    if document is None:
        # Most likely the upload's row isn't saved yet: the PDF reaches S3, which starts the
        # Lambda, just before POST /documents saves it. The Lambda retries a minute later.
        raise HTTPException(status_code=404, detail="No document has that s3_key")
    document.status = DocumentStatus.processed
    document.text_s3_key = update.text_s3_key
    db.commit()
    db.refresh(document)
    # After the commit, so the worker can't find the row before it says where the text is.
    # If Redis can't be reached, the 503 makes the Lambda try again, and that call queues it.
    try:
        queue.enqueue(EMBED_DOCUMENT, document.id, retry=EMBED_RETRY)
    except RedisError as error:
        logger.warning("Couldn't queue document %s for the worker: %s", document.id, error)
        raise HTTPException(status_code=503, detail="Couldn't queue the document. Try again.")
    logger.info("Document %s is processed, and queued for the worker", document.id)
    return document
