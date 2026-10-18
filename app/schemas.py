from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import TicketPriority, TicketStatus


class TicketCreate(BaseModel):
    """What a customer sends to open a ticket."""

    customer_name: str
    phone: str
    subject: str
    message: str


class TicketRead(TicketCreate):
    """What the API sends back: the fields above plus the ones the server fills in."""

    # Lets Pydantic read values straight off a database object (ticket.id, ticket.status, ...).
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime
    closed_at: datetime | None


class TicketUpdate(BaseModel):
    """What an agent sends to change a ticket. Leave out anything that isn't changing."""

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
