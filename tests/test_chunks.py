"""The chunks table holds pieces of help docs with their embeddings, and pgvector searches them."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DataError, IntegrityError

from app.models import EMBEDDING_DIMENSIONS, Chunk, Document


def embedding(*start):
    """An embedding of the right size: these numbers first, then zeros."""
    return [*start, *[0.0] * (EMBEDDING_DIMENSIONS - len(start))]


@pytest.fixture
def document(session_factory, make_agent):
    """A document's id, for chunks to belong to."""
    agent = make_agent()
    with session_factory() as db:
        document = Document(
            filename="manual.pdf",
            s3_key="uploads/manual.pdf",
            size_bytes=641,
            uploaded_by_id=agent.id,
        )
        db.add(document)
        db.commit()
        return document.id


def test_a_chunk_keeps_its_text_and_embedding(session_factory, document):
    # pgvector stores 4-byte floats, so these are numbers a 4-byte float holds exactly.
    vector = embedding(0.5, -0.25, 1.0)
    with session_factory() as db:
        db.add(
            Chunk(document_id=document, position=0, text="Refunds take 5 days.", embedding=vector)
        )
        db.commit()
    with session_factory() as db:
        chunk = db.scalars(select(Chunk)).one()
        assert chunk.text == "Refunds take 5 days."
        assert chunk.embedding == vector
        assert chunk.document.filename == "manual.pdf"
        assert chunk.created_at.utcoffset().total_seconds() == 0


def test_an_embedding_of_the_wrong_size_is_turned_away(session_factory, document):
    # Embeddings from another model, or a cut-off one, can't be compared with the rest.
    with session_factory() as db:
        db.add(Chunk(document_id=document, position=0, text="Hi", embedding=[1.0, 2.0, 3.0]))
        with pytest.raises(DataError, match=f"expected {EMBEDDING_DIMENSIONS} dimensions"):
            db.commit()


def test_a_place_in_a_document_holds_one_chunk(session_factory, document):
    with session_factory() as db:
        db.add(Chunk(document_id=document, position=0, text="First", embedding=embedding(1.0)))
        db.add(Chunk(document_id=document, position=0, text="Again", embedding=embedding(1.0)))
        with pytest.raises(IntegrityError, match="uq_chunks_document_id"):
            db.commit()


def test_deleting_a_document_deletes_its_chunks(session_factory, document):
    with session_factory() as db:
        db.add(Chunk(document_id=document, position=0, text="Gone", embedding=embedding(1.0)))
        db.commit()
    with session_factory() as db:
        db.delete(db.get(Document, document))
        db.commit()
        assert db.scalar(select(func.count()).select_from(Chunk)) == 0


def test_the_database_finds_the_chunks_closest_to_a_question(session_factory, document):
    texts = {
        "Refunds": embedding(1.0, 0.0),
        "Shipping": embedding(0.0, 1.0),
        "Both": embedding(1.0, 1.0),
    }
    with session_factory() as db:
        for position, (text, vector) in enumerate(texts.items()):
            db.add(Chunk(document_id=document, position=position, text=text, embedding=vector))
        db.commit()
        # Cosine distance compares the directions of two embeddings, not their lengths.
        question = embedding(0.9, 0.1)
        closest = db.scalars(select(Chunk.text).order_by(Chunk.embedding.cosine_distance(question)))
        assert closest.all() == ["Refunds", "Both", "Shipping"]
