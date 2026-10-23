"""The Lambda that pulls the text out of each help-doc PDF uploaded to S3.

S3 runs it for every new object under uploads/ that ends in .pdf (infra/aws/). It reads the
PDF, pulls out the text with pypdf, saves the text under text/ in the same bucket, and calls
the API, with the shared secret, to mark the document "processed". Its own output is under
text/, which the trigger ignores, so the function can't keep triggering itself.

It runs on Lambda's python3.14 runtime, which comes with boto3, so the zip that lambda/build.sh
makes holds only this file and pypdf. It can't import the app, which isn't in the zip, so the
tests check that the prefixes and the API's path here match the app's.
"""

import json
import logging
import os
from functools import cache
from io import BytesIO
from urllib.parse import unquote_plus
from urllib.request import Request, urlopen

import boto3
from pypdf import PdfReader

# Lambda sends everything the function logs to CloudWatch. Its runtime only shows warnings
# and worse unless a logger asks for INFO.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Where the API saves uploads (app/storage.py), and where the text goes.
UPLOAD_PREFIX = "uploads/"
TEXT_PREFIX = "text/"

# The API's endpoint for a processed document (app/routers/documents.py).
PROCESSED_PATH = "/webhooks/document-processed"
SECRET_HEADER = "X-Webhook-Secret"

# The function itself may run for 60 seconds. A slow API fails the call well before that.
API_TIMEOUT_SECONDS = 10


@cache
def s3_client():
    # Made once per container, on first use, and kept for the next invocations. Lambda sets
    # AWS_REGION, and gives boto3 the function's role's credentials.
    return boto3.client("s3")


@cache
def webhook_secret() -> str:
    """The shared secret, from Parameter Store. Read once per container, then kept."""
    ssm = boto3.client("ssm")
    response = ssm.get_parameter(Name=os.environ["SECRET_PARAMETER"], WithDecryption=True)
    return response["Parameter"]["Value"]


def text_key_for(pdf_key: str) -> str:
    """Where a PDF's text goes: uploads/<id>.pdf becomes text/<id>.txt."""
    name = pdf_key.removeprefix(UPLOAD_PREFIX).removesuffix(".pdf")
    return f"{TEXT_PREFIX}{name}.txt"


def pdf_to_text(pdf: bytes) -> str:
    """Every page's text, with a blank line between pages. Raises if pypdf can't read it."""
    reader = PdfReader(BytesIO(pdf))
    return "\n\n".join(page.extract_text() for page in reader.pages)


def mark_processed(pdf_key: str, text_key: str) -> None:
    """Tell the API the text is saved. Raises if the API answers with an error or can't be
    reached, so the invocation fails and Lambda tries it again."""
    request = Request(
        os.environ["API_URL"].rstrip("/") + PROCESSED_PATH,
        data=json.dumps({"s3_key": pdf_key, "text_s3_key": text_key}).encode(),
        headers={"Content-Type": "application/json", SECRET_HEADER: webhook_secret()},
        method="POST",
    )
    # urlopen raises HTTPError for any 4xx or 5xx answer.
    with urlopen(request, timeout=API_TIMEOUT_SECONDS):
        pass


def process(bucket: str, key: str) -> None:
    """Pull out one uploaded PDF's text, save it, and mark the document processed."""
    # The trigger only sends these. Anything else would mean the trigger was changed.
    if not (key.startswith(UPLOAD_PREFIX) and key.endswith(".pdf")):
        logger.warning("Skipped %s, which isn't an uploaded PDF", key)
        return

    pdf = s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        text = pdf_to_text(pdf)
    except Exception:
        # Only pypdf runs here, on bytes already in memory, so trying again would fail the
        # same way. A broken or encrypted file stays "uploaded", and the log says why.
        logger.exception("Couldn't read the text of %s", key)
        return
    if not text.strip():
        # Saved and marked processed anyway: there's nothing more to get without OCR.
        logger.warning("%s has no text to pull out, such as a scanned PDF", key)

    text_key = text_key_for(key)
    # Saving again on a retry writes the same text to the same key, so no harm is done.
    s3_client().put_object(
        Bucket=bucket,
        Key=text_key,
        Body=text.encode(),
        ContentType="text/plain; charset=utf-8",
    )
    mark_processed(key, text_key)
    logger.info("Saved the text of %s to %s: %d characters", key, text_key, len(text))


def handler(event, context):
    """Lambda's entry point. S3 calls it with one record per new object, usually one."""
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 sends the key URL-encoded, with a space as "+", so it has to be decoded.
        key = unquote_plus(record["s3"]["object"]["key"])
        process(bucket, key)
