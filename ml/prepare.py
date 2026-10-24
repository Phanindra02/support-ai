"""Clean the texts and split each dataset into training and test sets.

The split is the same on every run (a fixed seed), so the training scripts call split() again
rather than saving the sets to files.

uv run python -m ml.prepare
"""

import pandas as pd
from sklearn.model_selection import train_test_split

from ml.datasets import load_tickets, load_tweets
from ml.text import clean_text

# A fifth of each dataset is held back for testing, never seen in training or tuning.
TEST_SIZE = 0.2
SEED = 42


def clean(data: pd.DataFrame) -> pd.DataFrame:
    """Cleaned texts, without empty ones or repeats, so no text can land in both sets."""
    data = data.assign(text=data["text"].fillna("").map(clean_text))
    data = data[data["text"] != ""]
    return data.drop_duplicates("text").reset_index(drop=True)


def split(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Training and test sets, each with every label in the same share as the whole dataset."""
    train, test = train_test_split(
        data, test_size=TEST_SIZE, stratify=data["label"], random_state=SEED
    )
    return train.reset_index(drop=True), test.reset_index(drop=True)


def main() -> None:
    for name, load in [("Tickets", load_tickets), ("Tweets", load_tweets)]:
        raw = load()
        data = clean(raw)
        train, test = split(data)
        print(f"{name}: {len(raw)} rows, {len(data)} after cleaning")
        for part, rows in [("train", train), ("test", test)]:
            share = rows["label"].value_counts(normalize=True).round(3).to_dict()
            print(f"  {part}: {len(rows)} rows, {share}")


if __name__ == "__main__":
    main()
