"""The Lambda in lambda/extract_text.py: an uploaded PDF's text is saved, and the API is told.

S3 and Parameter Store are moto's fakes, and the Lambda's call to the API goes to the test app
instead of the internet, so a test runs the whole path: POST /documents, then the Lambda, then
POST /webhooks/document-processed. No test calls AWS.
"""

import contextlib
import json
import logging
import subprocess
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlsplit

import boto3
import extract_text
import pytest
from sqlalchemy import select

from app.config import PROJECT_ROOT, settings
from app.models import Document, DocumentStatus
from app.routers.documents import SECRET_HEADER as API_SECRET_HEADER
from app.routers.documents import webhook_router
from app.schemas import DocumentProcessed
from app.storage import UPLOAD_PREFIX

SECRET = "a-made-up-secret-for-the-tests-only-0123456789"
SECRET_PARAMETER = "/support-ai/LAMBDA_WEBHOOK_SECRET"
API_URL = "https://support.example.com"


def make_pdf(*page_texts: str) -> bytes:
    """A real PDF with one page of text per argument, written out by hand.

    A PDF is numbered objects (here a catalog, a page list, a font, and a page and its drawing
    commands for each text), then a table of where each object starts in the file.
    """
    page_ids = [4 + 2 * i for i in range(len(page_texts))]
    kids = b" ".join(b"%d 0 R" % page_id for page_id in page_ids)
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [%b] /Count %d >>" % (kids, len(page_ids)),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page_id, text in zip(page_ids, page_texts, strict=True):
        # Begin text, pick the font at 12 points, move to near the top left, draw, end text.
        commands = b"BT /F1 12 Tf 72 720 Td (%b) Tj ET" % text.encode("latin-1")
        objects[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (page_id + 1)
        )
        objects[page_id + 1] = b"<< /Length %d >>\nstream\n%b\nendstream" % (
            len(commands),
            commands,
        )

    pdf = bytearray(b"%PDF-1.4\n")
    starts = {}
    for number in sorted(objects):
        starts[number] = len(pdf)
        pdf += b"%d 0 obj\n%b\nendobj\n" % (number, objects[number])
    table_start = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for number in sorted(objects):
        pdf += b"%010d 00000 n \n" % starts[number]
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    pdf += b"startxref\n%d\n%%%%EOF\n" % table_start
    return bytes(pdf)


def s3_event(bucket: str, key: str) -> dict:
    """What S3 sends the function for a new object: the key URL-encoded, as S3 does."""
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": quote_plus(key)}}}]}


class ForwardToTestApp:
    """Stands in for urlopen: sends the Lambda's request to the test app, not the internet."""

    def __init__(self, client):
        self.client = client
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        url = urlsplit(request.full_url)
        response = self.client.request(
            request.get_method(),
            url.path,
            content=request.data,
            headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            # What urlopen raises for an error answer.
            raise HTTPError(request.full_url, response.status_code, response.text, None, None)
        return contextlib.nullcontext(response)


@pytest.fixture
def aws(s3, monkeypatch):
    """The fake S3 (with the test bucket) and Parameter Store, as the Lambda sees them."""
    # Lambda sets these for a real function: the region, and the two in lambda-function.json.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.setenv("API_URL", API_URL)
    monkeypatch.setenv("SECRET_PARAMETER", SECRET_PARAMETER)
    ssm = boto3.client("ssm", region_name="us-east-2")
    ssm.put_parameter(Name=SECRET_PARAMETER, Value=SECRET, Type="SecureString")
    # The Lambda keeps its clients and the secret for the container's life. Each test is a
    # new container, inside this test's fake AWS.
    extract_text.s3_client.cache_clear()
    extract_text.webhook_secret.cache_clear()
    yield s3
    extract_text.s3_client.cache_clear()
    extract_text.webhook_secret.cache_clear()


@pytest.fixture
def api(anonymous_client, monkeypatch):
    """The test app, set up with the same secret, answering the Lambda's calls."""
    monkeypatch.setattr(settings, "lambda_webhook_secret", SECRET)
    forward = ForwardToTestApp(anonymous_client)
    monkeypatch.setattr(extract_text, "urlopen", forward)
    return forward


def upload(client, pdf: bytes) -> Document:
    response = client.post("/documents", files={"file": ("manual.pdf", pdf, "application/pdf")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def stored(session_factory, document_id) -> Document:
    with session_factory() as db:
        return db.get(Document, document_id)


def text_in_s3(s3, bucket, key) -> str:
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()


def test_an_upload_has_its_text_saved_and_is_marked_processed(
    client, aws, api, test_bucket, session_factory
):
    document_id = upload(client, make_pdf("Returns are free within 30 days."))
    pdf_key = stored(session_factory, document_id).s3_key

    extract_text.handler(s3_event(test_bucket, pdf_key), None)

    document = stored(session_factory, document_id)
    assert document.status == DocumentStatus.processed
    assert document.text_s3_key == extract_text.text_key_for(pdf_key)
    assert text_in_s3(aws, test_bucket, document.text_s3_key) == "Returns are free within 30 days."
    saved = aws.head_object(Bucket=test_bucket, Key=document.text_s3_key)
    assert saved["ContentType"] == "text/plain; charset=utf-8"
    # The secret came from Parameter Store, and the call can't hang for the function's minute.
    [(request, timeout)] = api.requests
    assert request.full_url == API_URL + "/webhooks/document-processed"
    assert request.get_header(API_SECRET_HEADER.capitalize()) == SECRET
    assert timeout == extract_text.API_TIMEOUT_SECONDS < 60


def test_every_page_is_kept_in_order(client, aws, api, test_bucket, session_factory):
    pdf = make_pdf("Page one: shipping.", "Page two: returns.", "Page three: refunds.")
    document_id = upload(client, pdf)
    pdf_key = stored(session_factory, document_id).s3_key

    extract_text.handler(s3_event(test_bucket, pdf_key), None)

    text_key = stored(session_factory, document_id).text_s3_key
    assert text_in_s3(aws, test_bucket, text_key) == (
        "Page one: shipping.\n\nPage two: returns.\n\nPage three: refunds."
    )


def test_a_pdf_that_cant_be_read_is_logged_and_left_uploaded(
    client, aws, api, test_bucket, session_factory, caplog
):
    # It starts like a PDF, so the API took it, but pypdf can't read it.
    document_id = upload(client, b"%PDF-1.7\nnot really a PDF\n")
    pdf_key = stored(session_factory, document_id).s3_key

    with caplog.at_level(logging.ERROR, logger="extract_text"):
        # No exception: a retry would fail the same way, so it isn't asked for.
        extract_text.handler(s3_event(test_bucket, pdf_key), None)

    assert f"Couldn't read the text of {pdf_key}" in caplog.text
    assert stored(session_factory, document_id).status == DocumentStatus.uploaded
    assert api.requests == []
    listed = aws.list_objects_v2(Bucket=test_bucket, Prefix=extract_text.TEXT_PREFIX)
    assert listed["KeyCount"] == 0


def test_a_pdf_with_no_text_is_still_processed(
    client, aws, api, test_bucket, session_factory, caplog
):
    # Like a scanned page: a valid PDF with nothing written in it.
    document_id = upload(client, make_pdf(""))
    pdf_key = stored(session_factory, document_id).s3_key

    with caplog.at_level(logging.WARNING, logger="extract_text"):
        extract_text.handler(s3_event(test_bucket, pdf_key), None)

    assert "has no text to pull out" in caplog.text
    document = stored(session_factory, document_id)
    assert document.status == DocumentStatus.processed
    assert text_in_s3(aws, test_bucket, document.text_s3_key) == ""


def test_an_api_error_fails_the_run_so_lambda_retries_it(
    client, aws, api, test_bucket, session_factory
):
    # S3 can start the function before POST /documents has saved the row. The API answers
    # 404, the run fails, and Lambda tries again a minute later, when the row is there.
    pdf_key = UPLOAD_PREFIX + "0" * 32 + ".pdf"
    aws.put_object(Bucket=test_bucket, Key=pdf_key, Body=make_pdf("Too early."))

    with pytest.raises(HTTPError) as error:
        extract_text.handler(s3_event(test_bucket, pdf_key), None)
    assert error.value.code == 404

    me = client.get("/agents/me").json()
    with session_factory() as db:
        db.add(Document(filename="m.pdf", s3_key=pdf_key, size_bytes=1, uploaded_by_id=me["id"]))
        db.commit()
    extract_text.handler(s3_event(test_bucket, pdf_key), None)

    with session_factory() as db:
        document = db.scalar(select(Document).where(Document.s3_key == pdf_key))
    assert document.status == DocumentStatus.processed
    assert text_in_s3(aws, test_bucket, document.text_s3_key) == "Too early."


def test_an_unreachable_api_fails_the_run(client, aws, api, test_bucket, monkeypatch):
    pdf_key = UPLOAD_PREFIX + "0" * 32 + ".pdf"
    aws.put_object(Bucket=test_bucket, Key=pdf_key, Body=make_pdf("Anyone there?"))

    def unreachable(request, timeout):
        raise URLError("Connection refused")

    monkeypatch.setattr(extract_text, "urlopen", unreachable)
    with pytest.raises(URLError):
        extract_text.handler(s3_event(test_bucket, pdf_key), None)


def test_a_wrong_secret_fails_the_run(client, aws, api, test_bucket, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "lambda_webhook_secret", "not-the-same-secret-at-all-0123456789")
    document_id = upload(client, make_pdf("Hello."))
    pdf_key = stored(session_factory, document_id).s3_key

    with pytest.raises(HTTPError) as error:
        extract_text.handler(s3_event(test_bucket, pdf_key), None)
    assert error.value.code == 403
    assert stored(session_factory, document_id).status == DocumentStatus.uploaded


@pytest.mark.parametrize("key", ["text/abc.txt", "uploads/abc.txt", "other/abc.pdf"])
def test_only_uploaded_pdfs_are_read(aws, api, test_bucket, key, caplog):
    # The trigger only sends uploads/*.pdf. If it were changed to send the function's own
    # text/ output, the function would skip it instead of looping.
    with caplog.at_level(logging.WARNING, logger="extract_text"):
        extract_text.handler(s3_event(test_bucket, key), None)
    assert f"Skipped {key}" in caplog.text
    assert api.requests == []


def test_an_event_with_no_records_does_nothing(aws, api):
    extract_text.handler({}, None)
    assert api.requests == []


def test_the_key_from_s3_is_decoded(aws, api, test_bucket, monkeypatch):
    # S3 sends "uploads/my manual.pdf" as "uploads/my+manual.pdf".
    seen = []
    monkeypatch.setattr(extract_text, "process", lambda bucket, key: seen.append(key))
    extract_text.handler(s3_event(test_bucket, "uploads/my manual (2).pdf"), None)
    assert seen == ["uploads/my manual (2).pdf"]


# The Lambda can't import the app, so these check that its copies of the app's names match.


def test_the_lambda_reads_where_the_api_saves_uploads():
    assert extract_text.UPLOAD_PREFIX == UPLOAD_PREFIX


def test_the_lambda_calls_the_apis_endpoint_with_its_header():
    paths = {route.path for route in webhook_router.routes}
    assert extract_text.PROCESSED_PATH in paths
    assert extract_text.SECRET_HEADER == API_SECRET_HEADER


def test_the_text_key_is_one_the_api_accepts():
    pdf_key = UPLOAD_PREFIX + "70208fbd9d4a4cbc8cbbc7cb6f3b001c.pdf"
    text_key = extract_text.text_key_for(pdf_key)
    assert text_key == "text/70208fbd9d4a4cbc8cbbc7cb6f3b001c.txt"
    DocumentProcessed(s3_key=pdf_key, text_s3_key=text_key)


def test_the_body_the_lambda_sends_is_one_the_api_accepts():
    pdf_key = UPLOAD_PREFIX + "0" * 32 + ".pdf"
    body = {"s3_key": pdf_key, "text_s3_key": extract_text.text_key_for(pdf_key)}
    DocumentProcessed.model_validate_json(json.dumps(body))


# The zip that lambda/build.sh makes.


@pytest.fixture(scope="module")
def built_zip(tmp_path_factory):
    out = tmp_path_factory.mktemp("lambda-build")
    subprocess.run([PROJECT_ROOT / "lambda" / "build.sh", out], check=True, capture_output=True)
    with zipfile.ZipFile(out / "lambda.zip") as built:
        return built.namelist()


def test_the_zip_holds_the_handler_and_pypdf_at_its_top_level(built_zip):
    # Lambda's runtime imports extract_text from the zip's top level, the way lambda-function.json
    # names it (extract_text.handler), and pypdf from beside it.
    assert "extract_text.py" in built_zip
    assert "pypdf/__init__.py" in built_zip


def test_the_zip_leaves_out_what_the_runtime_has_and_the_app(built_zip):
    top_level = {name.split("/")[0] for name in built_zip}
    assert not top_level & {"boto3", "botocore", "app", "tests", ".lock"}
    assert not any("__pycache__" in name for name in built_zip)
