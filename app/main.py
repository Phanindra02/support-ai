from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import Ticket, TicketPriority, TicketStatus
from app.schemas import TicketCreate, TicketRead, TicketUpdate

# Create any missing tables when the app starts. Stage 2 replaces this with Alembic migrations.
Base.metadata.create_all(engine)

app = FastAPI(title="Support AI")

# Shortcut: any endpoint with a `db: SessionDep` parameter gets its own database session.
SessionDep = Annotated[Session, Depends(get_db)]


def get_ticket_or_404(db: Session, ticket_id: int) -> Ticket:
    """Look up a ticket by id, or stop the request with a 404 if it doesn't exist."""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@app.get("/")
def root():
    return {"message": "Support API is running"}


@app.post("/tickets", response_model=TicketRead, status_code=201)
def create_ticket(ticket_in: TicketCreate, db: SessionDep):
    ticket = Ticket(**ticket_in.model_dump())
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@app.get("/tickets", response_model=list[TicketRead])
def list_tickets(
    db: SessionDep,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = select(Ticket)
    if status is not None:
        query = query.where(Ticket.status == status)
    if priority is not None:
        query = query.where(Ticket.priority == priority)
    # Newest first. The id breaks ties between tickets created in the same second.
    query = query.order_by(Ticket.created_at.desc(), Ticket.id.desc())
    query = query.offset(skip).limit(limit)
    return db.scalars(query).all()


@app.get("/tickets/{ticket_id}", response_model=TicketRead)
def get_ticket(ticket_id: int, db: SessionDep):
    return get_ticket_or_404(db, ticket_id)


@app.patch("/tickets/{ticket_id}", response_model=TicketRead)
def update_ticket(ticket_id: int, ticket_in: TicketUpdate, db: SessionDep):
    ticket = get_ticket_or_404(db, ticket_id)
    # Keep only the fields that were sent. Missing or null fields stay as they are.
    changes = ticket_in.model_dump(exclude_none=True)

    # Stamp closed_at when a ticket closes, and clear it if the ticket is reopened.
    new_status = changes.get("status")
    if new_status == TicketStatus.closed and ticket.status != TicketStatus.closed:
        ticket.closed_at = datetime.now(timezone.utc)
    elif new_status is not None and new_status != TicketStatus.closed:
        ticket.closed_at = None

    for field, value in changes.items():
        setattr(ticket, field, value)
    db.commit()
    db.refresh(ticket)
    return ticket
