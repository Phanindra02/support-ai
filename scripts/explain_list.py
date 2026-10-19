"""Time the ticket list query with EXPLAIN ANALYZE, to measure what indexes change.

Run it from the project root, on a database seeded with 100,000 tickets:

    uv run python -m scripts.seed --reset --count 100000 --seed 42
    uv run python -m scripts.explain_list            # a table of median times
    uv run python -m scripts.explain_list --plans    # also print each query plan

EXPLAIN ANALYZE runs the query and reports how Postgres ran it and how long it took inside
the database. Network time and Python time aren't included.
"""

import argparse
import re
from statistics import median

from sqlalchemy import Connection, text

from app.database import engine
from app.routers.tickets import ticket_list_query
from app.models import TicketPriority, TicketStatus

RUNS = 7

# Matches the ways a plan reads a table, like "Index Scan Backward using ix_x on tickets".
SCAN = re.compile(r"(?:Parallel )?(?:Seq|Index|Index Only|Bitmap Heap|Bitmap Index) Scan[^(]*")

# Requests an agent's dashboard makes, as arguments to ticket_list_query.
CASES = {
    "first page, no filters": {},
    "status=open": {"status": TicketStatus.open},
    "priority=urgent": {"priority": TicketPriority.urgent},
    "status=open&priority=urgent": {
        "status": TicketStatus.open,
        "priority": TicketPriority.urgent,
    },
    "status=closed": {"status": TicketStatus.closed},
    "skip=50000 (a deep page)": {"skip": 50_000},
}


def explain(connection: Connection, arguments: dict) -> tuple[float, str]:
    """Run EXPLAIN ANALYZE on the list query once. Returns milliseconds and the plan."""
    query = ticket_list_query(**arguments)
    # literal_binds writes the values into the SQL, like 'open', so it can follow EXPLAIN.
    sql = query.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True})
    plan = "\n".join(connection.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}")).scalars())
    execution_time = re.search(r"Execution Time: ([\d.]+) ms", plan)
    if execution_time is None:
        raise ValueError("EXPLAIN ANALYZE output did not include an execution time")
    milliseconds = float(execution_time.group(1))
    return milliseconds, plan


def scans(plan: str) -> str:
    """Each different way the plan reads a table, joined with semicolons."""
    return "; ".join(dict.fromkeys(scan.strip() for scan in SCAN.findall(plan)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Time the ticket list query.")
    parser.add_argument("--plans", action="store_true", help="print each query plan")
    args = parser.parse_args()

    with engine.connect() as connection:
        count = connection.scalar(text("SELECT count(*) FROM tickets"))
        print(f"{count:,} tickets. Median of {RUNS} runs, after one warm-up run.\n")
        print(f"{'case':30} {'median ms':>10}  how Postgres reads the table")
        for name, arguments in CASES.items():
            explain(connection, arguments)  # Warm-up, so every run reads from memory.
            results = [explain(connection, arguments) for _ in range(RUNS)]
            milliseconds = median(ms for ms, _ in results)
            plan = results[-1][1]
            print(f"{name:30} {milliseconds:>10.2f}  {scans(plan)}")
            if args.plans:
                print(f"\n{plan}\n")


if __name__ == "__main__":
    main()
