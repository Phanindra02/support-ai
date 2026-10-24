"""Exporting the priority predictions next to the priority the team settled on."""

from app.models import ModelName, Prediction
from scripts.export_predictions import retraining_rows

SCORES = {"low": 0.1, "medium": 0.2, "high": 0.7}


def predict(session_factory, ticket_id, label, model=ModelName.priority):
    with session_factory() as db:
        db.add(Prediction(ticket_id=ticket_id, model=model, version=1, label=label, scores=SCORES))
        db.commit()


def rows(session_factory):
    with session_factory() as db:
        return retraining_rows(db)


def test_a_closed_ticket_shows_the_prediction_next_to_the_agents_priority(
    client, make_ticket, session_factory
):
    ticket = make_ticket(subject="Site down", message="Nobody can log in.")
    predict(session_factory, ticket["id"], "high")
    client.patch(f"/tickets/{ticket['id']}", json={"priority": "medium", "status": "closed"})

    assert rows(session_factory) == [
        {
            "ticket_id": ticket["id"],
            "text": "Site down\n\nNobody can log in.",
            "predicted": "high",
            "version": 1,
            "final": "medium",
            "changed_by_agent": True,
        }
    ]


def test_open_tickets_and_sentiment_predictions_are_left_out(client, make_ticket, session_factory):
    still_open = make_ticket()
    predict(session_factory, still_open["id"], "high")
    closed = make_ticket(phone="+16145550124")
    predict(session_factory, closed["id"], "negative", model=ModelName.sentiment)
    client.patch(f"/tickets/{closed['id']}", json={"status": "closed"})

    assert rows(session_factory) == []


def test_a_ticket_predicted_twice_counts_its_latest_prediction_once(
    client, make_ticket, session_factory
):
    ticket = make_ticket()
    predict(session_factory, ticket["id"], "low")
    predict(session_factory, ticket["id"], "high")
    client.patch(f"/tickets/{ticket['id']}", json={"priority": "high", "status": "closed"})

    [row] = rows(session_factory)
    assert (row["predicted"], row["final"], row["changed_by_agent"]) == ("high", "high", False)
