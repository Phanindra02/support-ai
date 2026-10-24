"""A first look at the datasets: class balance, text length, missing values, and duplicates.

uv run python -m ml.explore
"""

import pandas as pd

from ml.datasets import TICKETS_CSV, TWEETS_CSV, load_tickets, load_tweets


def summarize(data: pd.DataFrame) -> dict:
    """Numbers for a dataset with text and label columns."""
    text = data["text"].fillna("")
    words = text.str.split().str.len()
    return {
        "rows": len(data),
        "classes": data["label"].value_counts().to_dict(),
        "class_share": data["label"].value_counts(normalize=True).round(3).to_dict(),
        "missing": data.isna().sum().to_dict(),
        "empty_text": int((text.str.strip() == "").sum()),
        # A text in both the training and test sets would make the test too easy.
        "duplicate_texts": int(text.duplicated().sum()),
        "words": words.describe(percentiles=[0.5, 0.95]).round(1).to_dict(),
        "median_words_by_class": words.groupby(data["label"]).median().to_dict(),
    }


def main() -> None:
    for name, path, load in [
        ("Tickets", TICKETS_CSV, load_tickets),
        ("Tweets", TWEETS_CSV, load_tweets),
    ]:
        print(f"{name} ({path.name})")
        raw = pd.read_csv(path)
        print(f"  missing in the raw file: {raw.isna().sum()[lambda s: s > 0].to_dict()}")
        for key, value in summarize(load()).items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
