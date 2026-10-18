import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from redis import Redis
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.cache import get_redis, read_cache, write_cache
from app.database import get_db
from app.models import Customer, Message, MessageSender, Ticket, TicketPriority, TicketStatus
from app.schemas import (
    ErrorResponse,
    MessageRead,
    StatsRead,
    TicketCreate,
    TicketRead,
    TicketUpdate,
)


# Tables come from Alembic migrations (uv run alembic upgrade head), not from the app itself.
app = FastAPI(title="Support AI")

# Shortcut: any endpoint with a `db: SessionDep` parameter gets its own database session.
SessionDep = Annotated[Session, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]

# Where the stats are cached in Redis, and for how long.
STATS_KEY = "stats:tickets"
STATS_CACHE_SECONDS = 60

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


def find_customer(db: Session, phone: str) -> Customer | None:
    return db.scalar(select(Customer).where(Customer.phone == phone))


def get_or_create_customer(db: Session, name: str, phone: str) -> Customer:
    """Find the customer with this phone number, or add them if they're new.

    A returning customer keeps the name they gave first, even if they send a different one.
    """
    customer = find_customer(db, phone)
    if customer is not None:
        return customer

    # Two requests from a new number can both miss the lookup above, and both try to add the
    # customer. The unique constraint on phone rejects the second insert. The savepoint
    # undoes just that insert, instead of failing the whole request with a 500, and the
    # lookup then finds the customer the other request added.
    try:
        with db.begin_nested():
            customer = Customer(name=name, phone=phone)
            db.add(customer)
    except IntegrityError:
        customer = find_customer(db, phone)
    return customer


def ticket_list_query(
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    skip: int = 0,
    limit: int = 20,
) -> Select[tuple[Ticket]]:
    """The query behind GET /tickets. scripts/explain_list.py times this exact query."""
    # selectinload fetches the customers for the whole page in one more query. Without it,
    # reading each ticket's customer would run a query per ticket (the "N+1 queries" problem).
    query = select(Ticket).options(selectinload(Ticket.customer))
    if status is not None:
        query = query.where(Ticket.status == status)
    if priority is not None:
        query = query.where(Ticket.priority == priority)
    # Newest first. The id breaks ties between tickets created at the same moment.
    query = query.order_by(Ticket.created_at.desc(), Ticket.id.desc())
    return query.offset(skip).limit(limit)


def count_tickets(db: Session) -> StatsRead:
    """Count tickets by status, and the backlog (open or pending) by priority."""
    by_status = dict(db.execute(select(Ticket.status, func.count()).group_by(Ticket.status)).all())
    # IN (open, pending) rather than != closed, so Postgres can use the status index.
    backlog = dict(
        db.execute(
            select(Ticket.priority, func.count())
            .where(Ticket.status.in_([TicketStatus.open, TicketStatus.pending]))
            .group_by(Ticket.priority)
        ).all()
    )
    return StatsRead(
        by_status={status: by_status.get(status, 0) for status in TicketStatus},
        backlog_by_priority={priority: backlog.get(priority, 0) for priority in TicketPriority},
    )


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
    customer = get_or_create_customer(db, ticket_in.customer_name, ticket_in.phone)
    ticket = Ticket(customer=customer, subject=ticket_in.subject)
    # The customer's message starts the ticket's conversation.
    ticket.messages.append(Message(sender=MessageSender.customer, body=ticket_in.message))
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
    return db.scalars(ticket_list_query(status, priority, skip, limit)).all()


@app.get("/stats", response_model=StatsRead)
def get_stats(db: SessionDep, redis: RedisDep, response: Response):
    """Ticket counts for the dashboard, cached in Redis for 60 seconds.

    The X-Cache header says whether the counts came from the cache (HIT) or were just
    counted in the database (MISS).
    """
    # Cache-aside: look in the cache first. On a miss, count in the database and save the
    # result, so requests in the next 60 seconds skip the database.
    cached = read_cache(redis, STATS_KEY)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return StatsRead.model_validate_json(cached)

    stats = count_tickets(db)
    write_cache(redis, STATS_KEY, stats.model_dump_json(), STATS_CACHE_SECONDS)
    response.headers["X-Cache"] = "MISS"
    return stats


@app.get("/tickets/{ticket_id}", response_model=TicketRead, responses=NOT_FOUND)
def get_ticket(ticket_id: TicketId, db: SessionDep):
    return get_ticket_or_404(db, ticket_id)


@app.get("/tickets/{ticket_id}/messages", response_model=list[MessageRead], responses=NOT_FOUND)
def list_messages(ticket_id: TicketId, db: SessionDep):
    """The ticket's conversation, oldest message first."""
    get_ticket_or_404(db, ticket_id)
    query = (
        select(Message)
        .where(Message.ticket_id == ticket_id)
        .order_by(Message.created_at, Message.id)
    )
    return db.scalars(query).all()


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
