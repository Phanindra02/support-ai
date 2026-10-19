import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func, true
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

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="customer")


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

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="agent")


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
    messages: Mapped[list["Message"]] = relationship(back_populates="ticket")


class Message(Base):
    """One message in a ticket's conversation."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    sender: Mapped[MessageSender] = mapped_column(Enum(MessageSender, validate_strings=True))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped[Ticket] = relationship(back_populates="messages")
