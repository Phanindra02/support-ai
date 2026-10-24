"""Cleaning the texts and splitting them into training and test sets."""

import pandas as pd

from ml.prepare import clean, clean_text, split


def test_cleaning_drops_links_handles_escapes_and_extra_spaces():
    text = "@united  Lost my bag &amp; no reply!\nSee https://t.co/abc123 NOW"

    assert clean_text(text) == "lost my bag & no reply! see now"


def test_cleaning_drops_empty_texts_and_repeats():
    data = pd.DataFrame(
        {
            "text": ["Bag lost", "bag   LOST", "@united", None, "Great flight"],
            "label": ["negative", "negative", "neutral", "neutral", "positive"],
        }
    )

    assert clean(data).to_dict("records") == [
        {"text": "bag lost", "label": "negative"},
        {"text": "great flight", "label": "positive"},
    ]


def test_the_split_keeps_each_label_share_and_shares_no_rows():
    data = pd.DataFrame(
        {"text": [f"ticket {i}" for i in range(100)], "label": ["high"] * 60 + ["low"] * 40}
    )

    train, test = split(data)

    assert len(train) == 80 and len(test) == 20
    assert train["label"].value_counts().to_dict() == {"high": 48, "low": 32}
    assert test["label"].value_counts().to_dict() == {"high": 12, "low": 8}
    assert set(train["text"]).isdisjoint(test["text"])


def test_the_split_is_the_same_every_run():
    data = pd.DataFrame({"text": [f"t{i}" for i in range(50)], "label": ["a", "b"] * 25})

    assert split(data)[1]["text"].tolist() == split(data)[1]["text"].tolist()
