"""Train a model on a dataset's training set and score it on the test set.

uv run python -m ml.train tickets baseline
uv run python -m ml.train tickets xgboost
"""

import sys

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.pipeline import Pipeline, make_pipeline, make_union
from sklearn.preprocessing import FunctionTransformer, LabelEncoder
from xgboost import XGBClassifier

from ml.datasets import load_tickets, load_tweets
from ml.prepare import SEED, clean, split

DATASETS = {"tickets": load_tickets, "tweets": load_tweets}


def tfidf() -> TfidfVectorizer:
    """Word and word-pair counts, weighted so words in every text count for little.

    min_df=2 skips words seen in only one text, which can't generalize. sublinear_tf counts a
    word said five times as more than once, but not five times as much.
    """
    return TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)


def baseline() -> Pipeline:
    """TF-IDF and Logistic Regression: fast, simple, and the score to beat."""
    return make_pipeline(tfidf(), LogisticRegression(max_iter=1000))


def text_extras(texts) -> np.ndarray:
    """Numbers about each text that its words miss: its length, and how many ! and ? it has."""
    texts = pd.Series(texts)
    return np.column_stack(
        [texts.str.len(), texts.str.split().str.len(), texts.str.count("!"), texts.str.count(r"\?")]
    )


class LabeledXGBClassifier(XGBClassifier):
    """XGBoost only takes labels numbered 0, 1, 2. This one takes and returns label names."""

    def fit(self, X, y, **kwargs):
        self.encoder_ = LabelEncoder().fit(y)
        return super().fit(X, self.encoder_.transform(y), **kwargs)

    def predict(self, X, **kwargs):
        return self.encoder_.inverse_transform(super().predict(X, **kwargs))


def xgboost_model() -> Pipeline:
    """Boosted trees on the TF-IDF weights plus the text extras.

    Each tree corrects the mistakes of the trees before it, and trees can mix features in ways a
    linear model can't, like a long ticket that also says "urgent".
    """
    features = make_union(tfidf(), FunctionTransformer(text_extras))
    trees = LabeledXGBClassifier(
        n_estimators=300, learning_rate=0.1, max_depth=6, n_jobs=-1, random_state=SEED
    )
    return make_pipeline(features, trees)


MODELS = {"baseline": baseline, "xgboost": xgboost_model}


def evaluate(model: Pipeline, test: pd.DataFrame) -> dict:
    """Precision, recall, and F1 for each label, plus accuracy and the averages."""
    predicted = model.predict(test["text"])
    return classification_report(test["label"], predicted, output_dict=True, zero_division=0)


def print_report(report: dict) -> None:
    print(f"{'':>12} {'precision':>9} {'recall':>6} {'f1':>5} {'support':>7}")
    for label, scores in report.items():
        if label == "accuracy":
            print(f"{label:>12} {'':>9} {'':>6} {scores:>5.3f}")
            continue
        print(
            f"{label:>12} {scores['precision']:>9.3f} {scores['recall']:>6.3f}"
            f" {scores['f1-score']:>5.3f} {scores['support']:>7.0f}"
        )


def main(dataset: str, model_name: str) -> None:
    train, test = split(clean(DATASETS[dataset]()))
    model = MODELS[model_name]().fit(train["text"], train["label"])
    print(f"{model_name} on {dataset}: {len(train)} training rows, {len(test)} test rows")
    print_report(evaluate(model, test))


if __name__ == "__main__":
    main(*sys.argv[1:3])
