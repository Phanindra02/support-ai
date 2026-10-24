"""The dataset loaders and the exploration summary, on small made-up CSVs."""

import pandas as pd

from ml.datasets import load_tickets, load_tweets
from ml.explore import summarize


def test_tickets_are_english_only_with_subject_and_body_joined(tmp_path):
    path = tmp_path / "tickets.csv"
    pd.DataFrame(
        {
            "subject": ["Server down", None, "Rechnung"],
            "body": ["Nothing loads.", "Invoice is wrong.", "Falsch."],
            "priority": ["high", "low", "medium"],
            "language": ["en", "en", "de"],
        }
    ).to_csv(path, index=False)

    tickets = load_tickets(path)

    assert tickets.to_dict("records") == [
        {"text": "Server down\n\nNothing loads.", "label": "high"},
        {"text": "Invoice is wrong.", "label": "low"},
    ]


def test_tweets_keep_their_text_and_sentiment(tmp_path):
    path = tmp_path / "Tweets.csv"
    pd.DataFrame(
        {"text": ["@united lost my bag"], "airline_sentiment": ["negative"], "airline": ["United"]}
    ).to_csv(path, index=False)

    assert load_tweets(path).to_dict("records") == [
        {"text": "@united lost my bag", "label": "negative"}
    ]


def test_the_summary_counts_classes_lengths_gaps_and_duplicates():
    data = pd.DataFrame(
        {"text": ["a b c", "a b c", "d", None], "label": ["high", "high", "low", "low"]}
    )

    summary = summarize(data)

    assert summary["rows"] == 4
    assert summary["classes"] == {"high": 2, "low": 2}
    assert summary["class_share"] == {"high": 0.5, "low": 0.5}
    assert summary["missing"] == {"text": 1, "label": 0}
    assert summary["empty_text"] == 1
    assert summary["duplicate_texts"] == 1
    assert summary["median_words_by_class"] == {"high": 3.0, "low": 0.5}
