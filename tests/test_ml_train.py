"""Training the models and scoring them, on made-up texts with obvious labels."""

import pandas as pd

from ml.train import baseline, evaluate

TRAIN = pd.DataFrame(
    {
        "text": [f"server down outage {i}" for i in range(20)]
        + [f"question about pricing {i}" for i in range(20)],
        "label": ["high"] * 20 + ["low"] * 20,
    }
)
TEST = pd.DataFrame(
    {"text": ["the server is down", "a pricing question"], "label": ["high", "low"]}
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
