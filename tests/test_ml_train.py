"""Training the models and scoring them, on made-up texts with obvious labels."""

import pandas as pd

from ml.train import baseline, evaluate, text_extras, xgboost_model

# Both labels' texts are the same length, so only the words can tell them apart.
TRAIN = pd.DataFrame(
    {
        "text": [f"server down outage {i}" for i in range(20)]
        + [f"pricing plan query {i}" for i in range(20)],
        "label": ["high"] * 20 + ["low"] * 20,
    }
)
TEST = pd.DataFrame(
    {"text": ["the server is down", "a pricing plan query"], "label": ["high", "low"]}
)


def test_the_baseline_learns_which_words_go_with_each_label():
    model = baseline().fit(TRAIN["text"], TRAIN["label"])

    assert model.predict(TEST["text"]).tolist() == ["high", "low"]


def test_the_report_scores_each_label():
    model = baseline().fit(TRAIN["text"], TRAIN["label"])

    report = evaluate(model, TEST)

    assert report["accuracy"] == 1.0
    assert report["high"] == {"precision": 1.0, "recall": 1.0, "f1-score": 1.0, "support": 1.0}
    assert report["macro avg"]["f1-score"] == 1.0


def test_xgboost_learns_and_returns_label_names():
    model = xgboost_model().fit(TRAIN["text"], TRAIN["label"])

    assert model.predict(TEST["text"]).tolist() == ["high", "low"]


def test_the_extras_are_length_words_and_marks():
    assert text_extras(["help now!!", "why?"]).tolist() == [[10, 2, 2, 0], [4, 1, 0, 1]]
