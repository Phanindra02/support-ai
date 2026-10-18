import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import Ticket, TicketPriority, TicketStatus
from app.schemas import ErrorResponse, TicketCreate, TicketRead, TicketUpdate


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create any missing tables when the server starts, not when this file is imported, so
    # tests can import the app without touching support.db. Stage 2 replaces this with Alembic.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Support AI", lifespan=lifespan)

# Shortcut: any endpoint with a `db: SessionDep` parameter gets its own database session.
SessionDep = Annotated[Session, Depends(get_db)]

# The largest number a Postgres integer column can hold. No ticket id can be bigger, so no
# useful skip can be either, and bigger numbers would crash the database call with a 500.
MAX_INT = 2_147_483_647

# Ticket ids start at 1. Anything outside 1 to MAX_INT gets a 422 before the endpoint runs.
TicketId = Annotated[int, Path(ge=1, le=MAX_INT)]

# Lists the 404 response in /docs for endpoints that look up one ticket.
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Ticket not found"}}


def get_ticket_or_404(db: Session, ticket_id: int) -> Ticket:
    """Look up a ticket by id, or stop the request with a 404 if it doesn't exist."""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    """Return FastAPI's standard 422, even for input that can't be repeated back.

    The standard 422 repeats each bad input. Python's JSON parser accepts some input it can't
    write back out: NaN, Infinity, half of an emoji like "\\ud800", or lists nested 1,000
    deep. Repeating those crashed the error response itself with a 500.
    """
    try:
        return await request_validation_exception_handler(request, exc)
    except (ValueError, RecursionError):  # UnicodeEncodeError is a kind of ValueError.
        # Send the same errors without the input. loc can hold a JSON key the client sent, so
        # ensure_ascii=True writes any half-emoji in it as a plain "\ud800" escape.
        errors = [{"type": e["type"], "loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
        body = json.dumps({"detail": errors}, ensure_ascii=True)
        return Response(body, status_code=422, media_type="application/json")


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
    skip: Annotated[int, Query(ge=0, le=MAX_INT)] = 0,
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


@app.get("/tickets/{ticket_id}", response_model=TicketRead, responses=NOT_FOUND)
def get_ticket(ticket_id: TicketId, db: SessionDep):
    return get_ticket_or_404(db, ticket_id)


@app.patch("/tickets/{ticket_id}", response_model=TicketRead, responses=NOT_FOUND)
def update_ticket(ticket_id: TicketId, ticket_in: TicketUpdate, db: SessionDep):
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
