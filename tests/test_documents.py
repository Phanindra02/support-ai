"""POST /documents: agents upload help-doc PDFs, which go to S3 and get a row each.

S3 here is moto's fake (the s3 fixture), or one that can't be reached. No test calls AWS.
"""

import logging

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Document, DocumentStatus
from app.routers.documents import MAX_FILENAME_LENGTH, MAX_PDF_BYTES

# Only the first five bytes are checked, so this is enough to count as a PDF.
PDF = b"%PDF-1.7\n% A made-up PDF for the tests.\n%%EOF\n"


def upload(client, content=PDF, filename="manual.pdf", content_type="application/pdf"):
    return client.post("/documents", files={"file": (filename, content, content_type)})


def upload_raw(client, disposition: bytes, content=PDF):
    """Send a multipart body exactly as written, for filenames httpx would tidy up."""
    body = (
        b"--BOUNDARY\r\nContent-Disposition: form-data; name=\"file\"; " + disposition + b"\r\n"
        b"Content-Type: application/pdf\r\n\r\n" + content + b"\r\n--BOUNDARY--\r\n"
    )
    headers = {"Content-Type": "multipart/form-data; boundary=BOUNDARY"}
    return client.post("/documents", content=body, headers=headers)


def saved_documents(session_factory):
    with session_factory() as db:
        return db.scalars(select(Document).order_by(Document.id)).all()


def stored_keys(s3):
    """The key of every object in the fake S3, whatever bucket it's in."""
    buckets = [bucket["Name"] for bucket in s3.list_buckets()["Buckets"]]
    pages = [s3.list_objects_v2(Bucket=bucket) for bucket in buckets]
    return [item["Key"] for page in pages for item in page.get("Contents", [])]


def assert_nothing_saved(session_factory, s3=None):
    assert saved_documents(session_factory) == []
    if s3 is not None:
        assert stored_keys(s3) == []


def assert_bad_file(response, message):
    """FastAPI's standard 422, pointing at the file field."""
    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "file"]
    assert error["msg"] == message


def test_an_agent_uploads_a_pdf(client, s3, session_factory, test_bucket):
    me = client.get("/agents/me").json()

    response = upload(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "manual.pdf"
    assert body["size_bytes"] == len(PDF)
    assert body["status"] == "uploaded"
    assert body["uploaded_by_id"] == me["id"]
    assert body["created_at"]
    # Where the file is in S3 is the API's business, not the client's.
    assert "s3_key" not in body

    [document] = saved_documents(session_factory)
    assert document.id == body["id"]
    assert document.status == DocumentStatus.uploaded
    assert document.s3_key.startswith("uploads/") and document.s3_key.endswith(".pdf")

    stored = s3.get_object(Bucket=test_bucket, Key=document.s3_key)
    assert stored["Body"].read() == PDF
    assert stored["ContentType"] == "application/pdf"


def test_admins_can_upload_too(admin_client, s3):
    assert upload(admin_client).status_code == 201


def test_uploading_needs_a_login(anonymous_client, s3, session_factory):
    response = upload(anonymous_client)
    assert response.status_code == 401
    assert_nothing_saved(session_factory, s3)


def test_two_files_with_the_same_name_are_both_kept(client, s3, session_factory):
    upload(client, content=PDF + b"first")
    upload(client, content=PDF + b"second")

    first, second = saved_documents(session_factory)
    assert first.filename == second.filename == "manual.pdf"
    assert first.s3_key != second.s3_key
    assert sorted(stored_keys(s3)) == sorted([first.s3_key, second.s3_key])


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"Just some text, renamed to .pdf",
        b"%PDF",  # Cut off before the dash.
        b" %PDF-1.7",  # The signature has to come first.
        b"\x89PNG\r\n\x1a\n",
    ],
)
def test_a_file_that_isnt_a_pdf_is_refused(client, s3, session_factory, content):
    response = upload(client, content=content)
    assert_bad_file(response, "The file isn't a PDF")
    assert_nothing_saved(session_factory, s3)


def test_the_content_is_checked_not_the_content_type_the_client_claims(client, s3):
    assert upload(client, content_type="application/octet-stream").status_code == 201
    response = upload(client, content=b"plain text", content_type="application/pdf")
    assert_bad_file(response, "The file isn't a PDF")


def test_a_pdf_of_exactly_the_size_limit_is_accepted(client, s3):
    content = PDF + b"0" * (MAX_PDF_BYTES - len(PDF))
    response = upload(client, content=content)
    assert response.status_code == 201, response.text
    assert response.json()["size_bytes"] == MAX_PDF_BYTES


def test_a_pdf_over_the_size_limit_is_refused(client, s3, session_factory):
    content = PDF + b"0" * (MAX_PDF_BYTES - len(PDF) + 1)
    response = upload(client, content=content)
    assert response.status_code == 413
    assert response.json() == {"detail": "The file is over 10 MiB"}
    assert_nothing_saved(session_factory, s3)


def test_spaces_around_the_filename_are_trimmed(client, s3):
    assert upload(client, filename="  manual.pdf  ").json()["filename"] == "manual.pdf"


def test_a_filename_of_the_longest_length_is_accepted(client, s3):
    filename = "a" * (MAX_FILENAME_LENGTH - 4) + ".pdf"
    assert upload(client, filename=filename).json()["filename"] == filename


def test_a_filename_over_the_longest_length_is_refused(client, s3, session_factory):
    response = upload(client, filename="a" * (MAX_FILENAME_LENGTH - 3) + ".pdf")
    assert_bad_file(response, "The file's name can be at most 255 characters")
    assert_nothing_saved(session_factory, s3)


@pytest.mark.parametrize("disposition", [b'filename=""', b'filename="   "'])
def test_a_file_with_no_name_is_refused(client, s3, session_factory, disposition):
    assert_bad_file(upload_raw(client, disposition), "The file needs a name")
    assert_nothing_saved(session_factory, s3)


def test_a_filename_with_the_nul_character_is_refused(client, s3, session_factory):
    # Postgres can't store NUL, so saving it would crash with a 500.
    response = upload_raw(client, b'filename="bad\x00name.pdf"')
    assert_bad_file(response, "The file's name can't contain the NUL character (\\u0000)")
    assert_nothing_saved(session_factory, s3)


def test_a_filename_that_isnt_utf_8_is_stored_without_crashing(client, s3):
    # These bytes aren't valid UTF-8. Starlette reads them as Latin-1, so they arrive as text.
    response = upload_raw(client, b'filename="\xed\xa0\x80.pdf"')
    assert response.status_code == 201, response.text


def test_a_missing_file_is_a_standard_422(client, s3):
    response = client.post("/documents")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "file"]


def test_text_sent_in_place_of_a_file_is_a_standard_422(client, s3, session_factory):
    # A form field with no filename arrives as a string, not a file.
    response = client.post("/documents", data={"file": PDF.decode()})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "file"]
    assert_nothing_saved(session_factory, s3)


def test_uploads_are_turned_away_until_a_bucket_is_set(client, s3, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket", None)
    response = upload(client)
    assert response.status_code == 503
    assert response.json() == {"detail": "Uploads aren't set up: S3_BUCKET is empty"}
    assert_nothing_saved(session_factory, s3)


def test_a_bucket_that_s3_refuses_is_a_503(client, s3, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket", "a-bucket-that-does-not-exist")
    response = upload(client)
    assert response.status_code == 503
    assert response.json() == {"detail": "Couldn't save the file to S3. Try again."}
    assert_nothing_saved(session_factory, s3)


def test_an_unreachable_s3_is_a_503_and_a_warning(client, session_factory, caplog):
    # No s3 fixture, so the app gets the S3 on port 1, where nothing listens.
    with caplog.at_level(logging.WARNING, logger="app.routers.documents"):
        response = upload(client)
    assert response.status_code == 503
    assert "Couldn't save an upload to S3" in caplog.text
    assert_nothing_saved(session_factory)
