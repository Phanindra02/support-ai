from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.cache import RedisDep, clear_cache, read_cache, write_cache
from app.database import SessionDep
from app.models import Customer, Message, MessageSender, Ticket, TicketPriority, TicketStatus
from app.schemas import (
    MAX_INT,
    ErrorResponse,
    MessageRead,
    StatsRead,
    TicketCreate,
    TicketRead,
    TicketUpdate,
)

router = APIRouter(tags=["tickets"])

# Where the stats are cached in Redis, and for how long. Any change to a ticket also clears
# them, so the 60 seconds only matters if a clear fails or a change skips the API.
STATS_KEY = "stats:tickets"
STATS_CACHE_SECONDS = 60

# Ticket ids start at 1. Anything outside 1 to MAX_INT gets a 422 before the endpoint runs.
TicketId = Annotated[int, Path(ge=1, le=MAX_INT)]

# Lists the 404 response in /docs for endpoints that look up one ticket.
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Ticket not found"}
}


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
    except IntegrityError as exc:
        customer = find_customer(db, phone)
        if customer is None:
            raise exc
    return customer


def ticket_list_query(
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    skip: int = 0,
    limit: int = 20,
) -> Select[Ticket]:
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


@router.post("/tickets", response_model=TicketRead, status_code=201)
def create_ticket(ticket_in: TicketCreate, db: SessionDep, redis: RedisDep):
    customer = get_or_create_customer(db, ticket_in.customer_name, ticket_in.phone)
    ticket = Ticket(customer=customer, subject=ticket_in.subject)
    # The customer's message starts the ticket's conversation.
    ticket.messages.append(Message(sender=MessageSender.customer, body=ticket_in.message))
    db.add(ticket)
    db.commit()
    # After the commit, not before. Clearing first would let another request cache the old
    # counts again in the moment before the new ticket is saved.
    clear_cache(redis, STATS_KEY)
    db.refresh(ticket)
    return ticket


@router.get("/tickets", response_model=list[TicketRead])
def list_tickets(
    db: SessionDep,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    skip: Annotated[int, Query(ge=0, le=MAX_INT)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return db.scalars(ticket_list_query(status, priority, skip, limit)).all()


@router.get("/stats", response_model=StatsRead)
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


@router.get("/tickets/{ticket_id}", response_model=TicketRead, responses=NOT_FOUND)
def get_ticket(ticket_id: TicketId, db: SessionDep):
    return get_ticket_or_404(db, ticket_id)


@router.get(
    "/tickets/{ticket_id}/messages", response_model=list[MessageRead], responses=NOT_FOUND
)
def list_messages(ticket_id: TicketId, db: SessionDep):
    """The ticket's conversation, oldest message first."""
    get_ticket_or_404(db, ticket_id)
    query = (
        select(Message)
        .where(Message.ticket_id == ticket_id)
        .order_by(Message.created_at, Message.id)
    )
    return db.scalars(query).all()


@router.patch("/tickets/{ticket_id}", response_model=TicketRead, responses=NOT_FOUND)
def update_ticket(ticket_id: TicketId, ticket_in: TicketUpdate, db: SessionDep, redis: RedisDep):
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
    clear_cache(redis, STATS_KEY)  # The counts by status and priority may have changed.
    db.refresh(ticket)
    return ticket
