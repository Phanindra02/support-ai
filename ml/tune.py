"""Tune both priority models with cross-validation, with and without extra weight for rare labels.

Settings are chosen by their cross-validation score on the training set only. The test set
scores each model once at the end, so it stays a fair check on tickets no choice was made with.

uv run python -m ml.tune tickets
"""

import sys

import pandas as pd
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.utils.class_weight import compute_sample_weight

from ml.prepare import SEED, clean, split
from ml.train import DATASETS, MODELS, evaluate, print_report

GRIDS = {
    # C is how far the model may bend to fit the training tickets. Small C keeps the weights
    # small, which guards against learning words that only happen to go with one priority.
    "baseline": {"logisticregression__C": [0.3, 1, 3, 10, 30, 100]},
    # Shallow trees mix fewer features each, but more of them are needed.
    "xgboost": {"labeledxgbclassifier__max_depth": [3, 6]},
}
# 3 folds rather than 5, since each XGBoost fit takes about half a minute.
FOLDS = 3


def fit_params(model: Pipeline, labels: pd.Series, weighted: bool) -> dict:
    """Weights that make each label count the same in total, so the rare low tickets count more.

    Both models take sample weights, which XGBoost needs since it has no class_weight setting.
    """
    if not weighted:
        return {}
    step = model.steps[-1][0]
    return {f"{step}__sample_weight": compute_sample_weight("balanced", labels)}


def tune(name: str, train: pd.DataFrame, weighted: bool, folds: int = FOLDS) -> GridSearchCV:
    """Try each setting in the model's grid, then refit the best on all the training tickets."""
    model = MODELS[name]()
    search = GridSearchCV(
        model,
        GRIDS[name],
        # Macro F1 counts the rare low label as much as the others, unlike accuracy.
        scoring="f1_macro",
        cv=StratifiedKFold(n_splits=folds, shuffle=True, random_state=SEED),
    )
    return search.fit(train["text"], train["label"], **fit_params(model, train["label"], weighted))


def main(dataset: str) -> None:
    train, test = split(clean(DATASETS[dataset]()))
    results = []
    for name in GRIDS:
        for weighted in [False, True]:
            search = tune(name, train, weighted)
            report = evaluate(search.best_estimator_, test)
            results.append((search.best_score_, name, weighted, search.best_params_, report))
            print(
                f"{name}, weighted={weighted}: best {search.best_params_},"
                f" cross-validation macro F1 {search.best_score_:.3f},"
                f" test macro F1 {report['macro avg']['f1-score']:.3f},"
                f" test low recall {report['low']['recall']:.3f}"
            )
    score, name, weighted, params, report = max(results, key=lambda result: result[0])
    print(f"\nBest by cross-validation ({score:.3f}): {name}, weighted={weighted}, {params}")
    print_report(report)


if __name__ == "__main__":
    main(sys.argv[1])
