"""The EXPLAIN ANALYZE script times the same query GET /tickets runs."""

import pytest
from sqlalchemy import inspect, text

from scripts.explain_list import CASES, explain, scans


@pytest.mark.parametrize("name", CASES)
def test_every_case_runs_and_reports_a_time(engine, make_ticket, name):
    make_ticket()
    with engine.connect() as connection:
        milliseconds, plan = explain(connection, CASES[name])
    assert milliseconds >= 0
    assert "Execution Time" in plan


def test_explain_uses_the_filters_and_order_of_the_list_endpoint(engine):
    with engine.connect() as connection:
        _, plan = explain(connection, CASES["status=open&priority=urgent"])
    assert "status = 'open'::ticketstatus" in plan
    assert "priority = 'urgent'::ticketpriority" in plan
    assert "created_at DESC, id DESC" in plan


def test_scans_lists_each_way_a_table_is_read_once():
    plan = """Limit  (cost=0.29..1.81 rows=20 width=58)
  ->  Index Scan Backward using ix_tickets_created_at on tickets  (cost=0.29..7591.29)
  ->  Parallel Seq Scan on tickets  (cost=0.00..1820.24 rows=58824 width=58)
  ->  Parallel Seq Scan on tickets  (cost=0.00..1820.24 rows=58824 width=58)
  ->  Bitmap Index Scan on ix_tickets_status  (cost=0.00..52.98 rows=4633 width=0)"""
    assert scans(plan) == (
        "Index Scan Backward using ix_tickets_created_at on tickets; "
        "Parallel Seq Scan on tickets; "
        "Bitmap Index Scan on ix_tickets_status"
    )


def test_tickets_have_the_indexes_the_queries_need(engine):
    indexes = {ix["name"]: ix["column_names"] for ix in inspect(engine).get_indexes("tickets")}
    # The list's sort order, so the newest tickets can be read without sorting.
    assert indexes["ix_tickets_created_at_id"] == ["created_at", "id"]
    # For the stats counts by status.
    assert indexes["ix_tickets_status"] == ["status"]


@pytest.fixture
def many_tickets(engine, make_ticket):
    """5,000 tickets, enough that reading the whole table costs more than using an index."""
    customer_id = make_ticket()["customer"]["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tickets (customer_id, subject, status, priority, created_at)
                SELECT :customer_id, 'Order late', 'closed', 'low', now() - g * interval '1 minute'
                FROM generate_series(1, 5000) AS g
                """
            ),
            {"customer_id": customer_id},
        )
        # Refresh the table statistics the planner uses to choose a plan.
        connection.execute(text("ANALYZE tickets"))


@pytest.mark.parametrize("name", ["first page, no filters", "status=closed"])
def test_the_list_reads_the_newest_tickets_from_the_index_without_sorting(
    engine, many_tickets, name
):
    with engine.connect() as connection:
        _, plan = explain(connection, CASES[name])
    assert "Index Scan Backward using ix_tickets_created_at_id" in plan
    assert "Sort" not in plan
