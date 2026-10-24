"""Tuning the models with cross-validation and weighting the rare labels."""

import pandas as pd
import pytest

from ml.train import baseline
from ml.tune import GRIDS, fit_params, tune
from tests.test_ml_train import TRAIN


def test_weights_make_each_label_count_the_same_in_total():
    labels = pd.Series(["high", "high", "high", "low"])

    params = fit_params(baseline(), labels, weighted=True)

    weights = params["logisticregression__sample_weight"]
    assert weights.round(3).tolist() == [0.667, 0.667, 0.667, 2.0]
    assert weights[:3].sum() == pytest.approx(weights[3:].sum())


def test_no_weights_unless_asked():
    assert fit_params(baseline(), pd.Series(["high", "low"]), weighted=False) == {}


@pytest.mark.parametrize("name", ["baseline", "xgboost"])
@pytest.mark.parametrize("weighted", [False, True])
def test_tuning_picks_a_setting_from_the_grid_and_refits_it(name, weighted):
    search = tune(name, TRAIN, weighted, folds=2)

    [(setting, choices)] = GRIDS[name].items()
    assert search.best_params_[setting] in choices
    assert search.best_estimator_.predict(["server down outage 3"]).tolist() == ["high"]
