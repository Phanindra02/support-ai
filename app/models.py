import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# Lowercase member names on purpose: SQLAlchemy stores the member *name*,
# so the database holds "open" rather than "OPEN".
class TicketStatus(enum.StrEnum):
    open = "open"
    pending = "pending"
    closed = "closed"


class TicketPriority(enum.StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class ModelName(enum.StrEnum):
    """The model service's models (ml/service.py)."""

    priority = "priority"
    sentiment = "sentiment"


class MessageSender(enum.StrEnum):
    customer = "customer"
    agent = "agent"
    # The Stage 6 bot. Listed now, because adding a value to a Postgres enum type later
    # takes a migration of its own.
    bot = "bot"


class AgentRole(enum.StrEnum):
    # Agents work on tickets. Admins can do that too, and also manage the agents.
    agent = "agent"
    admin = "admin"


class DocumentStatus(enum.StrEnum):
    # The PDF is in S3, and its text hasn't been pulled out yet.
    uploaded = "uploaded"
    # The Lambda has saved the PDF's text in S3 (lambda/extract_text.py).
    processed = "processed"


class Customer(Base):
    """Someone who contacts support. Their phone number identifies them."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # One customer per number. The unique constraint also gives the column an index, so
    # finding a customer by phone, as every WhatsApp message will in Stage 3, is fast.
    phone: Mapped[str] = mapped_column(String(20), unique=True)  # E.164, e.g. +16145550123
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Ticket is defined further down. Python 3.14 only reads an annotation when something
    # asks for it (PEP 649), and SQLAlchemy asks once every model exists, so no quotes needed.
    tickets: Mapped[list[Ticket]] = relationship(back_populates="customer")


class Agent(Base):
    """A member of the support team, who logs in with their email and password."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # Lowercase, so Sam.Lee@example.com and sam.lee@example.com are the same login.
    email: Mapped[str] = mapped_column(String(254), unique=True)  # The longest valid address.
    # An Argon2 hash of the password, never the password itself. Empty means the agent can't
    # log in yet, like the agents the seed script makes.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Turns an account off without deleting it, because tickets still point to it.
    # server_default fills in the agents that already exist when the column is added.
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    role: Mapped[AgentRole] = mapped_column(
        Enum(AgentRole, validate_strings=True),
        default=AgentRole.agent,
        server_default=AgentRole.agent.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    tickets: Mapped[list[Ticket]] = relationship(back_populates="agent")


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        # In the same order the list sorts by (created_at, then id), so Postgres can read the
        # newest tickets straight from the index instead of sorting the whole table. Reading
        # the index backward gives newest first, so it doesn't need to be a DESC index.
        Index("ix_tickets_created_at_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Postgres doesn't index foreign keys by itself. These indexes make "this customer's
    # tickets" fast, and the joins between the tables.
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    # The agent handling the ticket. Empty until someone picks it up.
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    # validate_strings=True makes SQLAlchemy reject anything not on the list. The status index
    # serves the stats counts. Priority has none: measured, the planner never used one.
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, validate_strings=True), default=TicketStatus.open, index=True
    )
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, validate_strings=True), default=TicketPriority.medium
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer] = relationship(back_populates="tickets")
    agent: Mapped[Agent | None] = relationship(back_populates="tickets")
    messages: Mapped[list[Message]] = relationship(back_populates="ticket")


class Message(Base):
    """One message in a ticket's conversation."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    sender: Mapped[MessageSender] = mapped_column(Enum(MessageSender, validate_strings=True))
    body: Mapped[str] = mapped_column(Text)
    # Twilio's id for a message that came in on WhatsApp, like SM followed by 32 hex digits.
    # Twilio can deliver a message more than once, and unique means it's stored only once.
    # Empty for every other message. Postgres lets any number of rows leave it empty.
    twilio_sid: Mapped[str | None] = mapped_column(String(34), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped[Ticket] = relationship(back_populates="messages")


class Document(Base):
    """A help doc an agent uploaded. The PDF itself is in S3, and this row says where."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The name the file had on the agent's computer. Only ever shown, as the source of the
    # bot's answers in Stage 6, and never used as a path.
    filename: Mapped[str] = mapped_column(String(255))
    # Where the PDF is in the bucket, like uploads/<32 random hex digits>.pdf. The API makes
    # it up, so two files named manual.pdf can't overwrite each other. Unique, so the Lambda
    # can find the row from the key S3 tells it about.
    s3_key: Mapped[str] = mapped_column(String(255), unique=True)
    # Where the Lambda saved the PDF's text, like text/<the same hex digits>.txt. Empty until
    # the document is processed. The Stage 6 worker reads the text from here.
    text_s3_key: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int]
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, validate_strings=True), default=DocumentStatus.uploaded
    )
    # No index: nothing looks documents up by who uploaded them, and agents are never deleted.
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# How many numbers OpenAI's text-embedding-3-small returns for each piece of text. A different
# embedding model can return a different number, which takes a migration and embedding every
# chunk again: embeddings from two models can't be compared with each other.
EMBEDDING_DIMENSIONS = 1536


class Chunk(Base):
    """A piece of a help doc's text and its embedding, which the bot searches for answers."""

    __tablename__ = "chunks"
    # Each place in a document holds one chunk, so if the worker runs twice on the same
    # document, the second run can't save a second copy of every chunk.
    __table_args__ = (UniqueConstraint("document_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE, so deleting a document deletes its chunks too. The unique constraint above
    # starts with document_id, so it also serves as the index for finding a document's chunks.
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    # Where the chunk comes in its document, from 0.
    position: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    # The meaning of the text as 1,536 numbers. Texts that mean similar things get embeddings
    # that are close together, so the closest chunks to a question's embedding answer it best.
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped[Document] = relationship()


class Prediction(Base):
    """What a model said about a ticket when it opened, kept as data for retraining.

    Read next to the ticket's priority once an agent has closed it, a priority prediction
    shows what the model said and what the team decided (scripts/export_predictions.py).
    """

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    model: Mapped[ModelName] = mapped_column(Enum(ModelName, validate_strings=True))
    # The version that made the prediction, so a new version's can be told from an old one's.
    version: Mapped[int]
    label: Mapped[str] = mapped_column(String(20))
    # Every label's probability, not only the winner's, so a later check can see whether the
    # model grows less sure over time: a sign that new tickets look unlike the training ones.
    scores: Mapped[dict[str, float]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped[Ticket] = relationship()
