"""The EXPLAIN ANALYZE script times the same query GET /tickets runs."""

import pytest

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
