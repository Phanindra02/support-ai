"""The weekly report: counts per week, average time to close, and open tickets by priority."""

import pandas as pd
import pytest
from sqlalchemy import func, select

from app.models import Ticket, TicketStatus
from scripts.seed import add_fake_tickets
from scripts.weekly_report import COLUMNS, OPEN_COLUMNS, build_report, load_tickets, save_report

NOW = pd.Timestamp("2026-10-14 12:00", tz="UTC")  # A Wednesday.


def make_frame(rows):
    """Turn (priority, created_at, closed_at) rows into the DataFrame load_tickets returns."""
    tickets = pd.DataFrame(rows, columns=["priority", "created_at", "closed_at"])
    for column in ["created_at", "closed_at"]:
        tickets[column] = pd.to_datetime(tickets[column], utc=True)
    return tickets


@pytest.fixture
def two_weeks():
    """Five tickets over two weeks, with numbers that are easy to check by hand."""
    return make_frame(
        [
            ("high", "2026-10-05 09:00", "2026-10-05 19:00"),  # Week 1. Closed in 10 hours.
            ("low", "2026-10-06 10:00", "2026-10-12 10:00"),  # Week 1. Closes in week 2: 144 h.
            ("urgent", "2026-10-09 08:00", None),  # Week 1. Still open.
            ("urgent", "2026-10-13 09:00", "2026-10-13 13:00"),  # Week 2. Closed in 4 hours.
            ("medium", "2026-10-13 10:00", None),  # Week 2. Still open.
        ]
    )


def test_report_has_one_row_per_week_starting_monday(two_weeks):
    report = build_report(two_weeks, NOW)
    assert list(report.columns) == COLUMNS
    assert [str(week) for week in report["week_start"]] == ["2026-10-05", "2026-10-12"]


def test_report_counts_tickets_created_and_closed_each_week(two_weeks):
    report = build_report(two_weeks, NOW)
    assert report["tickets_created"].tolist() == [3, 2]
    assert report["tickets_closed"].tolist() == [1, 2]


def test_average_time_to_close_uses_the_tickets_closed_that_week(two_weeks):
    report = build_report(two_weeks, NOW)
    # Week 2 closes the 144-hour low ticket and the 4-hour urgent one: (144 + 4) / 2 = 74.
    assert report["avg_hours_to_close"].tolist() == [10.0, 74.0]


def test_open_tickets_are_counted_at_the_end_of_each_week(two_weeks):
    report = build_report(two_weeks, NOW)
    # Columns: low, medium, high, urgent, total. The low ticket was still open when week 1
    # ended, because it closed on the Monday of week 2.
    assert report.loc[0, [*OPEN_COLUMNS, "open_total"]].tolist() == [1, 0, 0, 1, 2]
    assert report.loc[1, [*OPEN_COLUMNS, "open_total"]].tolist() == [0, 1, 0, 1, 2]


def test_a_week_with_no_tickets_still_gets_a_row():
    tickets = make_frame(
        [
            ("low", "2026-09-29 09:00", None),
            ("high", "2026-10-13 09:00", None),
        ]
    )
    report = build_report(tickets, NOW)
    assert [str(week) for week in report["week_start"]] == [
        "2026-09-28",
        "2026-10-05",
        "2026-10-12",
    ]
    quiet_week = report.loc[1]
    assert quiet_week["tickets_created"] == 0
    assert quiet_week["tickets_closed"] == 0
    assert pd.isna(quiet_week["avg_hours_to_close"])
    assert quiet_week["open_low"] == 1


def test_no_tickets_gives_an_empty_report():
    report = build_report(make_frame([]), NOW)
    assert report.empty
    assert list(report.columns) == COLUMNS


def test_load_tickets_reads_times_as_utc(engine, session_factory):
    with session_factory() as db:
        db.add(
            Ticket(
                customer_name="Jane Doe",
                phone="+16145550123",
                subject="Order late",
                message="Where is my order?",
                created_at=pd.Timestamp("2026-10-05 09:00", tz="UTC").to_pydatetime(),
            )
        )
        db.commit()

    tickets = load_tickets(engine)
    assert tickets.loc[0, "created_at"] == pd.Timestamp("2026-10-05 09:00", tz="UTC")
    assert pd.isna(tickets.loc[0, "closed_at"])
    assert tickets.loc[0, "priority"] == "medium"


def test_report_on_seeded_data_adds_up(engine, session_factory, tmp_path):
    with session_factory() as db:
        add_fake_tickets(db, 500, seed=1)
        closed = db.scalar(
            select(func.count()).where(Ticket.status == TicketStatus.closed)
        )

    output = tmp_path / "reports" / "weekly_report.csv"
    report = save_report(engine, output, pd.Timestamp.now(tz="UTC"))

    assert report["tickets_created"].sum() == 500
    assert report["tickets_closed"].sum() == closed
    assert report["open_total"].iloc[-1] == 500 - closed
    # 12 weeks of tickets touch 12 or 13 Monday-to-Sunday weeks.
    assert len(report) in (12, 13)
    assert pd.read_csv(output).columns.tolist() == COLUMNS
