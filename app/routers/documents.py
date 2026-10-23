"""Help docs: agents upload PDFs here, and in Stage 6 the WhatsApp bot answers from them.

POST /documents saves the PDF to S3 and records it as "uploaded". Later in Stage 5, a Lambda
pulls the text out of each upload and marks it "processed".
"""

import logging
import os
from typing import Annotated

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError

from app.config import settings
from app.database import SessionDep
from app.models import Document
from app.schemas import DocumentRead, ErrorResponse
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

    # This runs after the whole upload has arrived, so it can't stop a huge upload from
    # filling the disk. Caddy, in front of the API later in Stage 5, will limit body size.
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
