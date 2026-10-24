"""The worker's job: split a processed document's text into chunks, embed them, and save them."""

import itertools

import pytest
from botocore.exceptions import ClientError
from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from rq import SimpleWorker
from rq.utils import import_attribute
from sqlalchemy import select

import worker.jobs
from app.config import settings
from app.embeddings import EMBEDDING_MODEL, get_embeddings
from app.models import EMBEDDING_DIMENSIONS, Chunk, Document, DocumentStatus
from app.queue import EMBED_DOCUMENT
from worker.jobs import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    chunk_document,
    embed_document,
    save_chunks,
    split_text,
)

PDF_KEY = "uploads/" + "a" * 32 + ".pdf"
TEXT_KEY = "text/" + "a" * 32 + ".txt"
SECRET = "a-made-up-secret-for-the-tests-only-0123456789"

# A few paragraphs, the way the Lambda saves a PDF's text, with a character that isn't ASCII.
TEXT = "\n\n".join(
    [
        "Refunds go back to the card you paid with, within 5 working days. " * 8,
        "Shipping is free on orders over $50, and takes 3 to 5 days. " * 8,
        "Café orders can't be returned once they've been opened. " * 4,
    ]
)


# Gives each text the same made-up embedding every time, without calling OpenAI.
fake_embeddings = DeterministicFakeEmbedding(size=EMBEDDING_DIMENSIONS)


class OpenAIIsDown(Embeddings):
    """Embeddings that fail, the way OpenAI's do when it can't be reached."""

    def embed_documents(self, texts):
        raise ConnectionError("Couldn't reach OpenAI")

    def embed_query(self, text):
        raise ConnectionError("Couldn't reach OpenAI")


def shared(before, after):
    """How many characters at the start of one chunk repeat the end of the one before."""
    return max(n for n in range(len(after) + 1) if before.endswith(after[:n]))


def test_a_short_text_is_one_chunk():
    assert split_text("Refunds take 5 days.") == ["Refunds take 5 days."]


def test_no_text_makes_no_chunks():
    # A scanned PDF holds pictures of text, so the Lambda finds none.
    assert split_text("") == []
    assert split_text("  \n\n ") == []


def test_a_long_text_is_cut_into_overlapping_chunks_of_at_most_chunk_size():
    words = [f"word{i}" for i in range(1000)]
    chunks = split_text(" ".join(words))
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)
    # No word is lost or cut in half.
    assert {word for chunk in chunks for word in chunk.split()} == set(words)
    # Each chunk starts by repeating up to CHUNK_OVERLAP characters of the one before.
    for before, after in itertools.pairwise(chunks):
        assert 0 < shared(before, after) <= CHUNK_OVERLAP


def test_paragraphs_are_kept_whole_where_they_fit():
    first, second, third = "A" * 600, "B" * 600, "C" * 300
    # The first two don't fit in 1,000 characters together, and the last two do.
    assert split_text(f"{first}\n\n{second}\n\n{third}") == [first, f"{second}\n\n{third}"]


@pytest.fixture
def processed_document(session_factory, make_agent):
    """A document's id, as the Lambda leaves it: processed, with its text's key."""
    agent = make_agent()
    with session_factory() as db:
        document = Document(
            filename="manual.pdf",
            s3_key=PDF_KEY,
            text_s3_key=TEXT_KEY,
            status=DocumentStatus.processed,
            size_bytes=641,
            uploaded_by_id=agent.id,
        )
        db.add(document)
        db.commit()
        return document.id


def test_the_job_reads_the_documents_text_from_s3_and_splits_it(
    s3, test_bucket, session_factory, processed_document
):
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=TEXT.encode())
    with session_factory() as db:
        chunks = chunk_document(db, s3, processed_document)
    assert chunks == split_text(TEXT)
    assert len(chunks) > 1
    assert "Café" in chunks[-1]


def test_a_document_that_isnt_there_fails_the_job(s3, session_factory):
    with session_factory() as db, pytest.raises(LookupError, match="No document has id 7"):
        chunk_document(db, s3, 7)


def test_a_document_that_isnt_processed_fails_the_job(s3, session_factory, make_agent):
    agent = make_agent()
    with session_factory() as db:
        document = Document(filename="a.pdf", s3_key=PDF_KEY, size_bytes=1, uploaded_by_id=agent.id)
        db.add(document)
        db.commit()
        with pytest.raises(ValueError, match="isn't processed yet"):
            chunk_document(db, s3, document.id)


def test_text_missing_from_s3_fails_the_job(s3, session_factory, processed_document):
    with session_factory() as db, pytest.raises(ClientError, match="NoSuchKey"):
        chunk_document(db, s3, processed_document)


def test_without_a_bucket_the_job_fails_with_a_clear_error(
    s3, session_factory, processed_document, monkeypatch
):
    monkeypatch.setattr(settings, "s3_bucket", None)
    with session_factory() as db, pytest.raises(RuntimeError, match="S3_BUCKET is empty"):
        chunk_document(db, s3, processed_document)


def saved_chunks(session_factory, document_id):
    """A document's chunks, in order, as (position, text, embedding)."""
    with session_factory() as db:
        chunks = db.scalars(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.position)
        )
        return [(chunk.position, chunk.text, chunk.embedding) for chunk in chunks]


def embedded(texts):
    """What the fake embeddings save for these texts. pgvector keeps 4-byte floats."""
    vectors = fake_embeddings.embed_documents(texts)
    return [
        (n, text, pytest.approx(v, abs=1e-6)) for n, (text, v) in enumerate(zip(texts, vectors))
    ]


def run_job(session_factory, s3, document_id, embeddings=fake_embeddings):
    with session_factory() as db:
        return save_chunks(db, s3, embeddings, document_id)


def test_each_chunk_is_saved_in_order_with_its_embedding(
    s3, test_bucket, session_factory, processed_document
):
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=TEXT.encode())
    texts = split_text(TEXT)
    assert run_job(session_factory, s3, processed_document) == len(texts)
    assert saved_chunks(session_factory, processed_document) == embedded(texts)


def test_running_the_job_twice_leaves_one_copy_of_each_chunk(
    s3, test_bucket, session_factory, processed_document
):
    # Queues deliver a job at least once, so it can run twice for the same document.
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=TEXT.encode())
    run_job(session_factory, s3, processed_document)
    run_job(session_factory, s3, processed_document)
    assert saved_chunks(session_factory, processed_document) == embedded(split_text(TEXT))


def test_new_text_replaces_the_old_chunks(s3, test_bucket, session_factory, processed_document):
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=TEXT.encode())
    run_job(session_factory, s3, processed_document)
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=b"Refunds take 5 days.")
    assert run_job(session_factory, s3, processed_document) == 1
    assert saved_chunks(session_factory, processed_document) == embedded(["Refunds take 5 days."])


def test_if_openai_fails_the_document_keeps_its_chunks(
    s3, test_bucket, session_factory, processed_document
):
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=TEXT.encode())
    run_job(session_factory, s3, processed_document)
    with pytest.raises(ConnectionError):
        run_job(session_factory, s3, processed_document, embeddings=OpenAIIsDown())
    assert saved_chunks(session_factory, processed_document) == embedded(split_text(TEXT))


def test_a_document_with_no_text_ends_up_with_no_chunks(
    s3, test_bucket, session_factory, processed_document
):
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=TEXT.encode())
    run_job(session_factory, s3, processed_document)
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=b"")
    # Nothing to embed, so OpenAI isn't called, and it being down doesn't matter.
    assert run_job(session_factory, s3, processed_document, embeddings=OpenAIIsDown()) == 0
    assert saved_chunks(session_factory, processed_document) == []


def test_embeddings_are_made_by_the_model_the_column_is_sized_for(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-made-up-for-the-tests")
    embeddings = get_embeddings()
    assert embeddings.model == EMBEDDING_MODEL == "text-embedding-3-small"
    assert embeddings.dimensions == EMBEDDING_DIMENSIONS == 1536
    assert embeddings.openai_api_key.get_secret_value() == "sk-made-up-for-the-tests"
    assert embeddings.request_timeout == 30


def test_without_a_key_nothing_is_embedded():
    # conftest.py empties the key for every test, so no test can call the real OpenAI.
    assert settings.openai_api_key is None
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is empty"):
        get_embeddings()


def test_the_api_queues_the_worker_s_real_job():
    # The API queues the job by its import path, so a typo would only show when a job ran.
    assert import_attribute(EMBED_DOCUMENT) is embed_document


def test_a_processed_upload_ends_up_as_chunks_with_embeddings(
    anonymous_client, s3, test_bucket, test_queue, session_factory, make_agent, monkeypatch
):
    # The whole path: the Lambda's call queues the job, and the worker runs it.
    monkeypatch.setattr(settings, "lambda_webhook_secret", SECRET)
    agent = make_agent()
    with session_factory() as db:
        db.add(
            Document(filename="manual.pdf", s3_key=PDF_KEY, size_bytes=641, uploaded_by_id=agent.id)
        )
        db.commit()
    s3.put_object(Bucket=test_bucket, Key=TEXT_KEY, Body=TEXT.encode())
    response = anonymous_client.post(
        "/webhooks/document-processed",
        json={"s3_key": PDF_KEY, "text_s3_key": TEXT_KEY},
        headers={"X-Webhook-Secret": SECRET},
    )
    assert response.status_code == 200, response.text

    # The job makes its own session, S3 client, and embeddings. Here they're the tests' ones.
    monkeypatch.setattr(worker.jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(worker.jobs, "get_s3_client", lambda: s3)
    monkeypatch.setattr(worker.jobs, "get_embeddings", lambda: fake_embeddings)
    [job] = test_queue.get_jobs()
    SimpleWorker([test_queue], connection=test_queue.connection).work(burst=True)
    assert job.get_status() == "finished", job.latest_result().exc_string
    assert job.return_value() == len(split_text(TEXT))
    with session_factory() as db:
        [document_id] = db.scalars(select(Document.id))
    assert saved_chunks(session_factory, document_id) == embedded(split_text(TEXT))
