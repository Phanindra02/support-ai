"""Train the two chosen models, save each with a version number, and upload them to S3.

Each model is saved as model.joblib with a metadata.json beside it, in
ml/models/<name>/v<version>/ (git-ignored), and with --upload in the help docs bucket at
models/<name>/v<version>/. A saved version is never changed: a new model gets a new version.

uv run python -m ml.save
uv run python -m ml.save --upload
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
import sklearn
from botocore.client import BaseClient
from sklearn.pipeline import Pipeline

from app.config import PROJECT_ROOT, settings
from ml.prepare import clean, split
from ml.train import DATASETS, MODELS, evaluate
from ml.tune import fit_params

MODELS_DIR = PROJECT_ROOT / "ml" / "models"
S3_PREFIX = "models/"


@dataclass(frozen=True)
class Chosen:
    """A model picked by ml.tune, and the version it's saved as."""

    version: int
    dataset: str
    model: str
    settings: dict
    weighted: bool


# The winners in docs/results.md. Bump the version whenever any of this changes.
CHOSEN = {
    "priority": Chosen(1, "tickets", "baseline", {"logisticregression__C": 30}, weighted=False),
    "sentiment": Chosen(1, "tweets", "baseline", {"logisticregression__C": 1}, weighted=True),
}


def fit(chosen: Chosen, train: pd.DataFrame) -> Pipeline:
    model = MODELS[chosen.model]().set_params(**chosen.settings)
    return model.fit(
        train["text"], train["label"], **fit_params(model, train["label"], chosen.weighted)
    )


def git_commit() -> str:
    """The commit the model was trained from, so its code can be found again."""
    # check=False: outside a git checkout, the commit is "unknown" rather than an error.
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    return result.stdout.strip() or "unknown"


def metadata(name: str, chosen: Chosen, train: pd.DataFrame, report: dict) -> dict:
    """What's needed to trust, compare, and reload the model later."""
    return {
        "name": name,
        "version": chosen.version,
        "dataset": chosen.dataset,
        "model": chosen.model,
        "settings": chosen.settings,
        "weighted": chosen.weighted,
        "labels": sorted(train["label"].unique()),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        # A joblib file only loads reliably in the scikit-learn version that saved it, so the
        # model service has to install this one.
        "sklearn_version": sklearn.__version__,
        "training_rows": len(train),
        "test_macro_f1": round(report["macro avg"]["f1-score"], 3),
        "test_f1": {
            label: round(report[label]["f1-score"], 3) for label in sorted(train["label"].unique())
        },
    }


def save(model: Pipeline, info: dict, models_dir: Path = MODELS_DIR) -> Path:
    """Write model.joblib and metadata.json to a folder for this name and version."""
    folder = models_dir / info["name"] / f"v{info['version']}"
    folder.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, folder / "model.joblib")
    (folder / "metadata.json").write_text(json.dumps(info, indent=2) + "\n")
    return folder


def upload(s3: BaseClient, bucket: str, folder: Path, models_dir: Path = MODELS_DIR) -> list[str]:
    """Copy the folder's files to the same path under models/ in the bucket."""
    keys = []
    for path in sorted(folder.iterdir()):
        key = S3_PREFIX + path.relative_to(models_dir).as_posix()
        s3.upload_file(str(path), bucket, key)
        keys.append(key)
    return keys


def main(argv: list[str]) -> None:
    for name, chosen in CHOSEN.items():
        train, test = split(clean(DATASETS[chosen.dataset]()))
        # Trained on the training set only, so the test score below is this exact model's.
        model = fit(chosen, train)
        info = metadata(name, chosen, train, evaluate(model, test))
        folder = save(model, info)
        size = (folder / "model.joblib").stat().st_size / 1_000_000
        print(f"{name} v{chosen.version}: test macro F1 {info['test_macro_f1']}, {size:.1f} MB")
        if "--upload" in argv:
            from app.storage import get_s3_client

            for key in upload(get_s3_client(), settings.s3_bucket, folder):
                print(f"  uploaded s3://{settings.s3_bucket}/{key}")


if __name__ == "__main__":
    main(sys.argv[1:])
