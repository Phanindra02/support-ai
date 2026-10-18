"""Weekly ticket report: volume, average time to close, and open tickets by priority.

Run it from the project root (seed the database first for interesting numbers):

    uv run python -m scripts.weekly_report                       # saves reports/weekly_report.csv
    uv run python -m scripts.weekly_report --output report.csv

Each row is one week, Monday to Sunday in UTC. The first and last weeks can be partial.
"""

import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, select

from app.database import engine as app_engine
from app.models import Ticket, TicketPriority

PRIORITIES = [priority.value for priority in TicketPriority]
OPEN_COLUMNS = [f"open_{priority}" for priority in PRIORITIES]
COLUMNS = [
    "week_start",
    "tickets_created",
    "tickets_closed",
    "avg_hours_to_close",
    *OPEN_COLUMNS,
    "open_total",
]


def load_tickets(engine: Engine) -> pd.DataFrame:
    """Read each ticket's priority, created_at, and closed_at into a DataFrame."""
    query = select(Ticket.priority, Ticket.created_at, Ticket.closed_at)
    with engine.connect() as connection:
        tickets = pd.read_sql(query, connection)
    tickets["priority"] = tickets["priority"].astype(str)
    # SQLite returns times without a timezone. They're stored in UTC, so label them UTC.
    for column in ["created_at", "closed_at"]:
        tickets[column] = pd.to_datetime(tickets[column], utc=True)
    return tickets


def week_start(times: pd.Series) -> pd.Series:
    """The Monday at 00:00 UTC that starts each time's week (Monday is weekday 0)."""
    return (times - pd.to_timedelta(times.dt.weekday, unit="D")).dt.normalize()


def build_report(tickets: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Summarize tickets by week, oldest week first.

    - tickets_created and tickets_closed: how many tickets opened and closed that week.
    - avg_hours_to_close: the average time to close for tickets closed that week. Grouping
      by the week a ticket closed, not opened, keeps recent weeks from looking fast just
      because their slow tickets haven't closed yet.
    - open_<priority> and open_total: tickets still open at the end of the week (or now, for
      the current week). Open means not closed yet, so pending tickets count too.
    """
    if tickets.empty:
        return pd.DataFrame(columns=COLUMNS)

    # Every Monday from the first ticket's week up to now, including weeks with no tickets.
    weeks = pd.date_range(week_start(tickets["created_at"]).min(), now, freq="7D")

    created = tickets.groupby(week_start(tickets["created_at"])).size()

    closed = tickets.dropna(subset=["closed_at"])
    hours_to_close = (closed["closed_at"] - closed["created_at"]).dt.total_seconds() / 3600
    closed_by_week = hours_to_close.groupby(week_start(closed["closed_at"]))

    open_counts = []
    for week in weeks:
        end = min(week + pd.Timedelta(days=7), now)
        still_open = (tickets["created_at"] < end) & (
            tickets["closed_at"].isna() | (tickets["closed_at"] >= end)
        )
        open_counts.append(tickets.loc[still_open, "priority"].value_counts())
    open_by_priority = (
        pd.DataFrame(open_counts, index=weeks).reindex(columns=PRIORITIES).fillna(0).astype(int)
    )

    report = pd.DataFrame(
        {
            "week_start": weeks.date,
            "tickets_created": created.reindex(weeks, fill_value=0).to_numpy(),
            "tickets_closed": closed_by_week.size().reindex(weeks, fill_value=0).to_numpy(),
            "avg_hours_to_close": closed_by_week.mean().reindex(weeks).round(1).to_numpy(),
        }
    )
    for priority, column in zip(PRIORITIES, OPEN_COLUMNS):
        report[column] = open_by_priority[priority].to_numpy()
    report["open_total"] = open_by_priority.sum(axis=1).to_numpy()
    return report


def save_report(engine: Engine, output: Path, now: pd.Timestamp) -> pd.DataFrame:
    """Build the report from the database and save it as a CSV."""
    report = build_report(load_tickets(engine), now)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output, index=False)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Save a weekly ticket report as a CSV.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/weekly_report.csv"),
        help="where to save the CSV (default reports/weekly_report.csv)",
    )
    args = parser.parse_args()

    report = save_report(app_engine, args.output, pd.Timestamp.now(tz="UTC"))
    if report.empty:
        print("No tickets yet. Add some with: uv run python -m scripts.seed")
        return
    print(report.to_string(index=False))
    print(f"\nSaved {len(report)} weeks to {args.output}")


if __name__ == "__main__":
    main()
