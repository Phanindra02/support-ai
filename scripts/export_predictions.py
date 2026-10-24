"""Write the priority model's predictions next to the priority the team settled on, as CSV.

Only closed tickets count, since an agent may still change an open ticket's priority. A row
where the two differ is a mistake the next model version can learn from.

uv run python -m scripts.export_predictions > predictions.csv
"""

import csv
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import ModelName, Prediction, Ticket, TicketStatus
from worker.jobs import first_message

COLUMNS = ["ticket_id", "text", "predicted", "version", "final", "changed_by_agent"]


def retraining_rows(db: Session) -> list[dict]:
    """One row per closed ticket with a priority prediction, oldest ticket first."""
    # RQ runs a job at least once, so a ticket may rarely have two predictions. The latest
    # counts.
    latest = (
        select(func.max(Prediction.id))
        .where(Prediction.model == ModelName.priority)
        .group_by(Prediction.ticket_id)
    )
    query = (
        select(Prediction, Ticket)
        .join(Ticket)
        .where(Prediction.id.in_(latest), Ticket.status == TicketStatus.closed)
        .order_by(Ticket.id)
    )
    return [
        {
            "ticket_id": ticket.id,
            # The text the model saw (worker/jobs.py).
            "text": f"{ticket.subject}\n\n{first_message(db, ticket)}".strip(),
            "predicted": prediction.label,
            "version": prediction.version,
            "final": ticket.priority.value,
            "changed_by_agent": prediction.label != ticket.priority,
        }
        for prediction, ticket in db.execute(query)
    ]


def main() -> None:
    with SessionLocal() as db:
        rows = retraining_rows(db)
    writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)


if __name__ == "__main__":
    main()
