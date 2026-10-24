"""Where saved models live: a folder per name and version, on disk and under models/ in S3.

ml/save.py writes and uploads them, and the model service (ml/service.py) downloads and loads
them, so both use this one layout.
"""

import json
from pathlib import Path

import joblib
import sklearn
from botocore.client import BaseClient
from sklearn.pipeline import Pipeline

# The project root, worked out here rather than imported from app.config, since the model
# service's image has none of the API's code.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "ml" / "models"
S3_PREFIX = "models/"
FILES = ("metadata.json", "model.joblib")


def model_folder(models_dir: Path, name: str, version: int) -> Path:
    return models_dir / name / f"v{version}"


def s3_key(name: str, version: int, filename: str) -> str:
    return f"{S3_PREFIX}{name}/v{version}/{filename}"


def download(s3: BaseClient, bucket: str, models_dir: Path, name: str, version: int) -> Path:
    folder = model_folder(models_dir, name, version)
    folder.mkdir(parents=True, exist_ok=True)
    for filename in FILES:
        s3.download_file(bucket, s3_key(name, version, filename), str(folder / filename))
    return folder


def load(models_dir: Path, name: str, version: int) -> Pipeline:
    """The saved model, refused if a different scikit-learn version saved it.

    joblib files are pickles: loading one runs code from the file, so only load them from the
    project's own private bucket. And they only load reliably in the version that wrote them.
    """
    folder = model_folder(models_dir, name, version)
    saved_with = json.loads((folder / "metadata.json").read_text())["sklearn_version"]
    if saved_with != sklearn.__version__:
        raise RuntimeError(
            f"{name} v{version} was saved with scikit-learn {saved_with}, "
            f"but {sklearn.__version__} is installed"
        )
    return joblib.load(folder / "model.joblib")
