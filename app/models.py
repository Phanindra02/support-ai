import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

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


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20))  # E.164 format, e.g. +16145550123
    subject: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    # validate_strings=True makes SQLAlchemy reject anything not on the list.
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, validate_strings=True), default=TicketStatus.open
    )
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, validate_strings=True), default=TicketPriority.medium
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
