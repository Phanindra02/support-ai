"""Saving the chosen models with a version number, and uploading them to S3."""

import json

import boto3
import joblib
import pytest
import sklearn
from moto import mock_aws

from ml.save import Chosen, fit, metadata, save, upload
from ml.train import evaluate
from tests.test_ml_train import TEST, TRAIN

CHOSEN = Chosen(3, "tickets", "baseline", {"logisticregression__C": 30}, weighted=True)
BUCKET = "support-ai-models-test"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-2")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-east-2"}
        )
        yield client


def test_a_saved_model_loads_back_with_its_metadata(tmp_path):
    model = fit(CHOSEN, TRAIN)
    info = metadata("priority", CHOSEN, TRAIN, evaluate(model, TEST))

    folder = save(model, info, tmp_path)

    assert folder == tmp_path / "priority" / "v3"
    loaded = joblib.load(folder / "model.joblib")
    assert loaded.predict(TEST["text"]).tolist() == ["high", "low"]
    assert loaded.get_params()["logisticregression__C"] == 30
    saved = json.loads((folder / "metadata.json").read_text())
    assert saved["version"] == 3
    assert saved["labels"] == ["high", "low"]
    assert saved["test_macro_f1"] == 1.0
    assert saved["sklearn_version"] == sklearn.__version__


def test_upload_puts_the_files_under_models_by_name_and_version(tmp_path, s3):
    folder = tmp_path / "priority" / "v3"
    folder.mkdir(parents=True)
    (folder / "model.joblib").write_bytes(b"model bytes")
    (folder / "metadata.json").write_text("{}")

    keys = upload(s3, BUCKET, folder, tmp_path)

    assert keys == ["models/priority/v3/metadata.json", "models/priority/v3/model.joblib"]
    body = s3.get_object(Bucket=BUCKET, Key="models/priority/v3/model.joblib")["Body"].read()
    assert body == b"model bytes"
