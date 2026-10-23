"""POST /webhooks/document-processed: the Lambda marks a document processed once its text is saved.

The Lambda itself is tested in test_extract_text.py, including a run through this endpoint.
"""

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Document, DocumentStatus
from app.schemas import TEXT_KEY_PATTERN, UPLOAD_KEY_PATTERN, DocumentProcessed
from app.storage import new_upload_key

SECRET = "a-made-up-secret-for-the-tests-only-0123456789"
PATH = "/webhooks/document-processed"
PDF_KEY = "uploads/70208fbd9d4a4cbc8cbbc7cb6f3b001c.pdf"
TEXT_KEY = "text/70208fbd9d4a4cbc8cbbc7cb6f3b001c.txt"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(settings, "lambda_webhook_secret", SECRET)
    return SECRET


@pytest.fixture
def document(session_factory, make_agent):
    """An uploaded document, as POST /documents leaves it."""
    agent = make_agent()
    with session_factory() as db:
        document = Document(
            filename="manual.pdf", s3_key=PDF_KEY, size_bytes=641, uploaded_by_id=agent.id
        )
        db.add(document)
        db.commit()
        return document.id


def send(client, body=None, secret=SECRET):
    body = {"s3_key": PDF_KEY, "text_s3_key": TEXT_KEY} if body is None else body
    headers = {} if secret is None else {"X-Webhook-Secret": secret}
    return client.post(PATH, json=body, headers=headers)


def stored(session_factory, document_id):
    with session_factory() as db:
        return db.get(Document, document_id)


def test_the_lambda_marks_a_document_processed(anonymous_client, secret, document, session_factory):
    response = send(anonymous_client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == document
    assert body["status"] == "processed"
    # Where things are in S3 is the API's business, not the client's.
    assert "s3_key" not in body and "text_s3_key" not in body
    saved = stored(session_factory, document)
    assert saved.status == DocumentStatus.processed
    assert saved.text_s3_key == TEXT_KEY


def test_marking_it_again_changes_nothing(anonymous_client, secret, document, session_factory):
    # Lambda retries, and S3 can send the same event twice, so a repeat must be harmless.
    first = send(anonymous_client)
    second = send(anonymous_client)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert stored(session_factory, document).text_s3_key == TEXT_KEY


def test_an_unknown_upload_is_a_404(anonymous_client, secret):
    response = send(anonymous_client)
    assert response.status_code == 404
    assert response.json() == {"detail": "No document has that s3_key"}


@pytest.mark.parametrize(
    "given",
    # The last is sent as UTF-8 bytes, as a real client would. The test client only takes
    # ASCII text in a header.
    [None, "", "wrong", SECRET[:-1], SECRET + "x", SECRET.upper(), ("é" + SECRET).encode()],
    ids=["missing", "empty", "wrong", "too-short", "too-long", "wrong-case", "non-ascii"],
)
def test_anything_but_the_secret_is_a_403(
    anonymous_client, secret, document, session_factory, given
):
    response = send(anonymous_client, secret=given)
    assert response.status_code == 403
    assert response.json() == {"detail": "Wrong or missing X-Webhook-Secret"}
    assert stored(session_factory, document).status == DocumentStatus.uploaded


def test_a_header_that_isnt_text_is_a_403_not_a_500(anonymous_client, secret):
    # Header bytes that aren't valid UTF-8 arrive as Latin-1 text. compare_digest gets bytes,
    # so it never raises on them.
    response = anonymous_client.post(
        PATH,
        json={"s3_key": PDF_KEY, "text_s3_key": TEXT_KEY},
        headers={"X-Webhook-Secret": b"\xff\xfe"},
    )
    assert response.status_code == 403


def test_the_secret_is_checked_before_the_body(anonymous_client, secret):
    # Without the secret, a caller learns nothing about what a valid body looks like.
    response = send(anonymous_client, body={"nonsense": True}, secret=None)
    assert response.status_code == 403


def test_nothing_is_accepted_until_the_secret_is_set(anonymous_client, monkeypatch):
    monkeypatch.setattr(settings, "lambda_webhook_secret", None)
    response = send(anonymous_client, secret="")
    assert response.status_code == 503
    assert "LAMBDA_WEBHOOK_SECRET" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"s3_key": PDF_KEY},
        {"text_s3_key": TEXT_KEY},
        {"s3_key": PDF_KEY, "text_s3_key": TEXT_KEY, "status": "uploaded"},
        {"s3_key": "uploads/../secret.pdf", "text_s3_key": TEXT_KEY},
        {"s3_key": PDF_KEY.upper(), "text_s3_key": TEXT_KEY},
        {"s3_key": PDF_KEY + "\n", "text_s3_key": TEXT_KEY},
        {"s3_key": PDF_KEY, "text_s3_key": "uploads/70208fbd9d4a4cbc8cbbc7cb6f3b001c.pdf"},
        {"s3_key": PDF_KEY, "text_s3_key": TEXT_KEY.replace(".txt", ".txt\x00")},
        {"s3_key": 1, "text_s3_key": TEXT_KEY},
        [],
    ],
    ids=[
        "no-text-key",
        "no-pdf-key",
        "extra-field",
        "path-in-key",
        "uppercase",
        "newline",
        "text-key-is-a-pdf",
        "nul",
        "number",
        "not-an-object",
    ],
)
def test_bad_input_is_a_422(anonymous_client, secret, document, session_factory, body):
    response = send(anonymous_client, body=body)
    assert response.status_code == 422, response.text
    assert stored(session_factory, document).status == DocumentStatus.uploaded


def test_the_keys_the_api_makes_are_accepted():
    # The pattern must accept every upload key app/storage.py makes, or uploads would never
    # be marked processed.
    key = new_upload_key()
    text_key = "text/" + key.removeprefix("uploads/").replace(".pdf", ".txt")
    DocumentProcessed(s3_key=key, text_s3_key=text_key)
    assert UPLOAD_KEY_PATTERN.startswith("^uploads/") and TEXT_KEY_PATTERN.startswith("^text/")


def test_only_the_lambdas_documents_change(anonymous_client, secret, document, session_factory):
    # A second document stays as it was.
    other_key = "uploads/" + "0" * 32 + ".pdf"
    with session_factory() as db:
        agent_id = db.get(Document, document).uploaded_by_id
        db.add(
            Document(filename="other.pdf", s3_key=other_key, size_bytes=1, uploaded_by_id=agent_id)
        )
        db.commit()

    send(anonymous_client)

    with session_factory() as db:
        statuses = dict(db.execute(select(Document.s3_key, Document.status)).all())
    assert statuses == {PDF_KEY: DocumentStatus.processed, other_key: DocumentStatus.uploaded}
