"""Fill the database with realistic fake agents, customers, tickets, and messages.

Run it from the project root, after creating the tables with `uv run alembic upgrade head`:

    uv run python -m scripts.seed                  # add 1,000 tickets
    uv run python -m scripts.seed --count 100000   # add a different number
    uv run python -m scripts.seed --seed 42        # the same data every run
    uv run python -m scripts.seed --reset          # delete all existing data first
"""

import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone

from faker import Faker
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from app.cache import clear_cache, redis_client
from app.database import SessionLocal, engine
from app.main import STATS_KEY
from app.models import (
    Agent,
    Customer,
    Message,
    MessageSender,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.schemas import TicketCreate

WEEKS = 12
AGENT_COUNT = 12

# Customers open 1.6 tickets each on average, so some come back with new problems.
TICKETS_PER_CUSTOMER = 1.6

# Tickets are saved this many at a time, so memory stays flat even for 100,000 tickets.
BATCH_SIZE = 5_000

# Phone numbers are +1, an area code, then 555-0100 to 555-0199. Numbers from 555-0100 to
# 555-0199 are set aside for fiction, so these never belong to a real person.
AREA_CODES = range(201, 990)
PHONE_COUNT = len(AREA_CODES) * 100

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

# How often a customer says thanks before their ticket closes.
THANKS_RATE = 0.4
THANKS = [
    "Thanks, that fixed it!",
    "Got it, thank you.",
    "Perfect, thanks for the quick help.",
    "That worked. I appreciate it!",
]

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

# (subject, customer message, agent reply). The {placeholders} get filled in with fake details.
TOPICS = [
    (
        "Where is my order?",
        "I ordered the {product} on {date} and it still hasn't arrived. The tracking page "
        "hasn't changed in days. My order number is {order}.",
        "Sorry for the wait. I checked with the carrier, and order {order} is moving again. "
        "It should reach you within two business days.",
    ),
    (
        "Refund for order {order}",
        "I sent the {product} back two weeks ago but haven't seen a refund yet. Can you check?",
        "Thanks for your patience. Your refund for order {order} has been issued, and it should "
        "show on your card in 3 to 5 business days.",
    ),
    (
        "Charged twice",
        "My card was charged ${amount} twice for order {order}. Please refund the extra charge.",
        "You're right, order {order} was charged twice. I've refunded the extra ${amount}.",
    ),
    (
        "Can't log in",
        "I've tried to reset my password three times, but the email never arrives. My account "
        "email is {email}.",
        "Sorry about that. I've sent a new reset link to {email}. If it isn't in your inbox, "
        "please check your spam folder.",
    ),
    (
        "My {product} arrived damaged",
        "The box was crushed and the {product} inside is broken. I'd like a replacement.",
        "I'm sorry it arrived broken. A replacement {product} ships today, and you don't need "
        "to send the damaged one back.",
    ),
    (
        "Wrong item delivered",
        "I ordered the {product} but got something else. Order number {order}.",
        "Sorry for the mix-up. The {product} is on its way, and a prepaid return label for the "
        "wrong item is in your email.",
    ),
    (
        "Change my delivery address",
        "I moved last week. Can you send order {order} to my new address instead?",
        "Done. Order {order} will now go to your new address.",
    ),
    (
        "Cancel my subscription",
        "Please cancel my subscription before the next billing date. I'm being charged "
        "${amount} a month and don't use it anymore.",
        "Your subscription is canceled, and you won't be charged again.",
    ),
    (
        "Question about the {product}",
        "Does the {product} come with a warranty? I'd like to know before I order one.",
        "Yes, the {product} comes with a one-year warranty that covers any defects.",
    ),
    (
        "App keeps crashing",
        "The app crashes every time I open my order history. I'm on the latest version.",
        "Thanks for reporting this. A fix is coming in the next app update. Until then, "
        "reinstalling the app stops the crash.",
    ),
    (
        "Discount code not working",
        "The code {code} says it's invalid at checkout, but your email said it works until "
        "the end of the month.",
        "Sorry about that. The code {code} works again, and I've extended it by a week.",
    ),
]


def phone_number(n: int) -> str:
    """The nth fake phone number, for n from 0 to PHONE_COUNT - 1. Each n gives its own."""
    area_code, line = divmod(n, 100)
    return f"+1{AREA_CODES[area_code]}55501{line:02d}"


def random_times(rng: random.Random, count: int, now: datetime) -> list[datetime]:
    """Random moments in the last 12 weeks, oldest first."""
    seconds = WEEKS * 7 * 24 * 60 * 60
    return sorted(now - timedelta(seconds=rng.uniform(0, seconds)) for _ in range(count))


def make_agents(fake: Faker, now: datetime) -> list[Agent]:
    """The support team, hired before the first ticket."""
    agents = []
    for _ in range(AGENT_COUNT):
        first, last = fake.unique.first_name(), fake.last_name()
        agents.append(
            Agent(
                name=f"{first} {last}",
                # example.com is set aside for examples, so these addresses reach no one.
                email=f"{first}.{last}@example.com".lower(),
                created_at=now - timedelta(weeks=WEEKS + 4),
            )
        )
    return agents


def make_ticket(fake: Faker, rng: random.Random, created_at: datetime, now: datetime) -> Ticket:
    """Make one fake ticket and its messages. The caller sets its customer and agent."""
    order_date = created_at - timedelta(days=rng.randint(2, 10))
    subject, message, reply = rng.choice(TOPICS)
    details = {
        "product": rng.choice(PRODUCTS),
        "order": fake.bothify("SA-#####"),
        "date": f"{order_date:%B} {order_date.day}",
        "amount": f"{rng.uniform(9, 250):.2f}",
        "email": fake.email(),
        "code": fake.bothify("SAVE##"),
    }

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

    ticket = Ticket(
        subject=subject.format(**details),
        status=status,
        priority=priority,
        created_at=created_at,
        closed_at=closed_at,
    )
    first_message = Message(
        sender=MessageSender.customer, body=message.format(**details), created_at=created_at
    )
    ticket.messages.append(first_message)

    # Open tickets are still waiting for an agent. Pending and closed ones got a reply.
    if status == TicketStatus.open:
        return ticket
    if closed_at is None:
        replied_at = min(created_at + timedelta(hours=rng.uniform(0.5, 12)), now)
    else:
        replied_at = created_at + (closed_at - created_at) * rng.uniform(0.2, 0.8)
    ticket.messages.append(
        Message(sender=MessageSender.agent, body=reply.format(**details), created_at=replied_at)
    )
    if closed_at is not None and rng.random() < THANKS_RATE:
        thanked_at = replied_at + (closed_at - replied_at) * rng.uniform(0.1, 0.9)
        ticket.messages.append(
            Message(sender=MessageSender.customer, body=rng.choice(THANKS), created_at=thanked_at)
        )
    return ticket


def make_tickets(count: int, seed: int | None = None, now: datetime | None = None) -> list[Ticket]:
    """Make fake tickets in memory, oldest first, without customers or agents.

    The same seed and the same `now` give the same tickets.
    """
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)
    return [make_ticket(fake, rng, created_at, now) for created_at in random_times(rng, count, now)]


def add_fake_tickets(
    db: Session,
    count: int,
    seed: int | None = None,
    reset: bool = False,
    now: datetime | None = None,
) -> None:
    """Save fake tickets with their customers, agents, and messages, in one transaction.

    If reset is True, delete all existing data first.
    """
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)

    if reset:
        # Children before parents, so no foreign key points at a deleted row.
        for model in [Message, Ticket, Customer, Agent]:
            db.execute(delete(model))

    # Use the team that's already there, or hire one.
    agent_ids = list(db.scalars(select(Agent.id)))
    if not agent_ids:
        agents = make_agents(fake, now)
        db.add_all(agents)
        db.flush()  # Sends the INSERTs, so the agents get their ids.
        agent_ids = [agent.id for agent in agents]

    # The customers tickets can come from. Picking from them at random means some customers
    # open several tickets. Only the ones who open a ticket are saved. Numbers that existing
    # customers already have are left out, because each number belongs to one customer.
    taken = set(db.scalars(select(Customer.phone)))
    free = [n for n in range(PHONE_COUNT) if phone_number(n) not in taken]
    if not free:
        raise ValueError("Every fake phone number is taken. Run again with --reset.")
    pool_size = min(len(free), max(1, round(count / TICKETS_PER_CUSTOMER)))
    phones = [phone_number(n) for n in rng.sample(free, pool_size)]
    names = [fake.name() for _ in range(pool_size)]
    saved_customer_ids: dict[int, int] = {}  # Position in the pool -> id in the database.

    times = random_times(rng, count, now)
    for start in range(0, count, BATCH_SIZE):
        new_customers: dict[int, Customer] = {}
        for created_at in times[start : start + BATCH_SIZE]:
            ticket = make_ticket(fake, rng, created_at, now)
            pick = rng.randrange(pool_size)
            # Check the fake details against the same rules the API uses.
            TicketCreate(
                customer_name=names[pick],
                phone=phones[pick],
                subject=ticket.subject,
                message=ticket.messages[0].body,
            )

            if pick in saved_customer_ids:
                ticket.customer_id = saved_customer_ids[pick]
            else:
                if pick not in new_customers:
                    # Tickets are made oldest first, so this is the customer's first ticket.
                    new_customers[pick] = Customer(
                        name=names[pick], phone=phones[pick], created_at=created_at
                    )
                ticket.customer = new_customers[pick]

            if ticket.status != TicketStatus.open:
                ticket.agent_id = rng.choice(agent_ids)
            db.add(ticket)

        db.flush()
        saved_customer_ids.update({pick: customer.id for pick, customer in new_customers.items()})
        # Forget the objects already sent to the database, so memory doesn't grow each batch.
        db.expunge_all()

    db.commit()


def count_rows(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Add realistic fake tickets to the database.")
    parser.add_argument("--count", type=int, default=1000, help="tickets to add (default 1000)")
    parser.add_argument("--seed", type=int, help="make the same data every run")
    parser.add_argument("--reset", action="store_true", help="delete all existing data first")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")

    if not inspect(engine).has_table(Ticket.__tablename__):
        sys.exit("The tables don't exist yet. Create them with: uv run alembic upgrade head")
    with SessionLocal() as db:
        add_fake_tickets(db, args.count, args.seed, args.reset)
        tickets, customers = count_rows(db, Ticket), count_rows(db, Customer)
    # The cached ticket counts are out of date now.
    clear_cache(redis_client, STATS_KEY)
    print(
        f"Added {args.count:,} tickets. "
        f"The database now has {tickets:,} tickets from {customers:,} customers."
    )


if __name__ == "__main__":
    main()
