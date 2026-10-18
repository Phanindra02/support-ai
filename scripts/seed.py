"""Fill the database with realistic fake tickets, so the weekly report has real numbers.

Run it from the project root:

    uv run python -m scripts.seed               # add 1,000 tickets to the database
    uv run python -m scripts.seed --count 5000  # add a different number
    uv run python -m scripts.seed --seed 42     # the same tickets every run
    uv run python -m scripts.seed --reset       # delete every ticket first
"""

import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone

from faker import Faker
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine
from app.models import Ticket, TicketPriority, TicketStatus
from app.schemas import TicketCreate

WEEKS = 12

# How tickets usually split by priority: mostly routine, a few urgent.
PRIORITY_WEIGHTS = {
    TicketPriority.low: 30,
    TicketPriority.medium: 45,
    TicketPriority.high: 18,
    TicketPriority.urgent: 7,
}

# The typical (median) hours to close a ticket. Urgent tickets get handled first.
MEDIAN_HOURS_TO_CLOSE = {
    TicketPriority.urgent: 3,
    TicketPriority.high: 10,
    TicketPriority.medium: 30,
    TicketPriority.low: 60,
}

# Some tickets stall, waiting on a customer who never replies, so not every old ticket closes.
STALL_RATE = 0.05

PRODUCTS = [
    "wireless headphones",
    "coffee maker",
    "running shoes",
    "phone case",
    "desk lamp",
    "backpack",
    "smart watch",
    "blender",
]

# (subject, message) pairs. The {placeholders} get filled in with fake details.
TOPICS = [
    (
        "Where is my order?",
        "I ordered the {product} on {date} and it still hasn't arrived. The tracking page "
        "hasn't changed in days. My order number is {order}.",
    ),
    (
        "Refund for order {order}",
        "I sent the {product} back two weeks ago but haven't seen a refund yet. Can you check?",
    ),
    (
        "Charged twice",
        "My card was charged ${amount} twice for order {order}. Please refund the extra charge.",
    ),
    (
        "Can't log in",
        "I've tried to reset my password three times, but the email never arrives. My account "
        "email is {email}.",
    ),
    (
        "My {product} arrived damaged",
        "The box was crushed and the {product} inside is broken. I'd like a replacement.",
    ),
    (
        "Wrong item delivered",
        "I ordered the {product} but got something else. Order number {order}.",
    ),
    (
        "Change my delivery address",
        "I moved last week. Can you send order {order} to my new address instead?",
    ),
    (
        "Cancel my subscription",
        "Please cancel my subscription before the next billing date. I'm being charged "
        "${amount} a month and don't use it anymore.",
    ),
    (
        "Question about the {product}",
        "Does the {product} come with a warranty? I'd like to know before I order one.",
    ),
    (
        "App keeps crashing",
        "The app crashes every time I open my order history. I'm on the latest version.",
    ),
    (
        "Discount code not working",
        "The code {code} says it's invalid at checkout, but your email said it works until "
        "the end of the month.",
    ),
]


def make_ticket(fake: Faker, rng: random.Random, now: datetime) -> Ticket:
    """Make one fake ticket, created at a random time in the last 12 weeks."""
    created_at = now - timedelta(seconds=rng.uniform(0, WEEKS * 7 * 24 * 60 * 60))
    order_date = created_at - timedelta(days=rng.randint(2, 10))
    subject, message = rng.choice(TOPICS)
    details = {
        "product": rng.choice(PRODUCTS),
        "order": fake.bothify("SA-#####"),
        "date": f"{order_date:%B} {order_date.day}",
        "amount": f"{rng.uniform(9, 250):.2f}",
        "email": fake.email(),
        "code": fake.bothify("SAVE##"),
    }

    # Check the fake details against the same rules the API uses.
    fields = TicketCreate(
        customer_name=fake.name(),
        # 555-0100 to 555-0199 are set aside for fiction, so these never belong to a real person.
        phone=f"+1{rng.randint(201, 989)}55501{rng.randint(0, 99):02d}",
        subject=subject.format(**details),
        message=message.format(**details),
    ).model_dump()

    priority = rng.choices(list(PRIORITY_WEIGHTS), weights=list(PRIORITY_WEIGHTS.values()))[0]

    # Times to close are skewed: most land near the typical time, and a few take far longer.
    # A log-normal distribution has that shape, and its median is easy to set.
    hours = rng.lognormvariate(math.log(MEDIAN_HOURS_TO_CLOSE[priority]), 0.8)
    closed_at = created_at + timedelta(hours=hours)

    # A ticket is closed only if its close time has already passed, so older tickets are
    # mostly closed and this week's are mostly still open.
    if closed_at <= now and rng.random() >= STALL_RATE:
        status = TicketStatus.closed
    else:
        # Still being worked on: open, or pending while waiting on the customer.
        status = rng.choices([TicketStatus.open, TicketStatus.pending], weights=[65, 35])[0]
        closed_at = None

    return Ticket(
        **fields, status=status, priority=priority, created_at=created_at, closed_at=closed_at
    )


def make_tickets(count: int, seed: int | None = None, now: datetime | None = None) -> list[Ticket]:
    """Make fake tickets. The same seed and the same `now` give the same tickets."""
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)
    return [make_ticket(fake, rng, now) for _ in range(count)]


def add_fake_tickets(
    db: Session, count: int, seed: int | None = None, reset: bool = False
) -> None:
    """Save fake tickets to the database, after deleting every ticket if reset is True."""
    if reset:
        db.execute(delete(Ticket))
    db.add_all(make_tickets(count, seed))
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Add realistic fake tickets to the database.")
    parser.add_argument("--count", type=int, default=1000, help="tickets to add (default 1000)")
    parser.add_argument("--seed", type=int, help="make the same tickets every run")
    parser.add_argument("--reset", action="store_true", help="delete every ticket first")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")

    if not inspect(engine).has_table(Ticket.__tablename__):
        sys.exit("The tables don't exist yet. Create them with: uv run alembic upgrade head")
    with SessionLocal() as db:
        add_fake_tickets(db, args.count, args.seed, args.reset)
        total = db.scalar(select(func.count()).select_from(Ticket))
    print(f"Added {args.count} tickets. The database now has {total}.")


if __name__ == "__main__":
    main()
