"""Load the two Kaggle datasets (ml/data/, git-ignored) as text and a label.

Tickets: Tobias Bueck's "Multilingual Customer Support Tickets", English rows only, with a
priority of low, medium, or high. Tweets: CrowdFlower's "Twitter US Airline Sentiment",
labeled negative, neutral, or positive. See docs/stages/07-ml.md for where to download them.
"""

from pathlib import Path

import pandas as pd

from app.config import PROJECT_ROOT

DATA = PROJECT_ROOT / "ml" / "data"
# The newest and largest version of the tickets, with the most English rows.
TICKETS_CSV = DATA / "tickets" / "aa_dataset-tickets-multi-lang-5-2-50-version.csv"
TWEETS_CSV = DATA / "sentiment" / "Tweets.csv"


def ticket_text(subject: pd.Series, body: pd.Series) -> pd.Series:
    """The subject, a blank line, then the body, the way a ticket reads. Many have no subject."""
    subject = subject.fillna("").str.strip()
    body = body.fillna("").str.strip()
    return (subject + "\n\n" + body).str.strip()


def load_tickets(path: Path = TICKETS_CSV) -> pd.DataFrame:
    """English tickets as text and priority columns."""
    raw = pd.read_csv(path)
    english = raw[raw["language"] == "en"]
    return pd.DataFrame(
        {"text": ticket_text(english["subject"], english["body"]), "label": english["priority"]}
    ).reset_index(drop=True)


def load_tweets(path: Path = TWEETS_CSV) -> pd.DataFrame:
    """Tweets as text and sentiment columns."""
    raw = pd.read_csv(path)
    return pd.DataFrame({"text": raw["text"], "label": raw["airline_sentiment"]})
