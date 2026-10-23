"""Request bodies over the limit get a 413 as they arrive, before login or parsing."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.body_limit import BodySizeLimit
from app.main import MAX_BODY_BYTES
from app.routers.documents import MAX_PDF_BYTES

LIMIT = 10


@pytest.fixture(scope="module")
def small_app():
    """An app that takes at most 10 bytes, so the tests don't need megabytes."""
    app = FastAPI()
    app.add_middleware(BodySizeLimit, max_bytes=LIMIT)

    @app.post("/json")
    def take_json(item: dict):
        # FastAPI reads and parses this body itself, before the endpoint runs.
        return item

    @app.post("/raw")
    async def take_raw(request: Request):
        body = await request.body()
        return {"size": len(body), "content_length": request.headers.get("content-length")}

    return TestClient(app)


def chunks(*parts: bytes):
    """A body given as a generator, which httpx sends chunked, with no Content-Length."""
    yield from parts


def test_a_body_up_to_the_limit_gets_through(small_app):
    assert small_app.post("/raw", content=b"x" * LIMIT).json()["size"] == LIMIT


def test_a_body_that_says_it_is_too_big_is_refused(small_app):
    response = small_app.post("/raw", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.json() == {"detail": "The request is too large"}


def test_a_chunked_body_that_grows_too_big_is_refused(small_app):
    # Check the test sends what it means to: a body with no Content-Length.
    assert small_app.post("/raw", content=chunks(b"12345")).json()["content_length"] is None

    response = small_app.post("/raw", content=chunks(b"123456", b"789012"))
    assert response.status_code == 413
    assert response.json() == {"detail": "The request is too large"}


def test_a_chunked_body_too_big_for_fastapis_own_parsing_is_a_413_not_a_400(small_app):
    # FastAPI turns most errors from reading a body into a 400. An HTTPException goes through.
    response = small_app.post(
        "/json",
        content=chunks(b'{"a": ', b'"long value"}'),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_the_apps_limit_leaves_room_for_a_10_mib_pdf_in_a_form():
    assert MAX_PDF_BYTES < MAX_BODY_BYTES <= MAX_PDF_BYTES + 64 * 1024


def test_a_huge_upload_is_refused_before_login(anonymous_client):
    # A 401 here would mean the whole body was read before the login check.
    content = b"%PDF-" + b"0" * MAX_BODY_BYTES
    response = anonymous_client.post("/documents", files={"file": ("big.pdf", content)})
    assert response.status_code == 413


def test_a_huge_json_body_is_refused_before_it_is_read_into_memory(client):
    response = client.post(
        "/tickets", content=b"x" * (MAX_BODY_BYTES + 1), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
