"""The model service, with small models trained on made-up texts in a temporary folder."""

import json

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from ml import service
from ml.registry import s3_key
from ml.save import Chosen, fit, metadata, save
from ml.service import Settings
from ml.train import evaluate
from tests.test_ml_train import TEST, TRAIN

BUCKET = "support-ai-models-test"


def save_models(models_dir, version=1):
    """A priority and a sentiment model, both the made-up high and low one, saved as version."""
    chosen = Chosen(version, "tickets", "baseline", {}, weighted=False)
    model = fit(chosen, TRAIN)
    report = evaluate(model, TEST)
    for name in ["priority", "sentiment"]:
        save(model, metadata(name, chosen, TRAIN, report), models_dir)


def client_for(monkeypatch, settings):
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    return TestClient(service.app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    save_models(tmp_path)
    with client_for(monkeypatch, Settings(models_dir=tmp_path, _env_file=None)) as client:
        yield client


@pytest.mark.parametrize("name", ["priority", "sentiment"])
def test_a_prediction_names_the_label_its_scores_and_the_model(client, name):
    response = client.post(f"/predict/{name}", json={"text": "The server is DOWN!"})

    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "high"
    assert body["scores"].keys() == {"high", "low"}
    assert sum(body["scores"].values()) == pytest.approx(1, abs=0.001)
    assert body["scores"]["high"] > 0.5
    assert (body["model"], body["version"]) == (name, 1)


def test_the_text_is_cleaned_the_way_it_was_for_training(client):
    # The handle and the link are dropped, as they were in training, leaving the pricing words.
    response = client.post(
        "/predict/priority", json={"text": "@united PRICING plan QUERY https://t.co/x"}
    )

    assert response.json()["label"] == "low"


@pytest.mark.parametrize(
    "body", [{"text": ""}, {"text": "x" * 10_001}, {}, {"text": "hi", "extra": 1}]
)
def test_bad_input_is_a_422(client, body):
    assert client.post("/predict/priority", json=body).status_code == 422


def test_health_names_the_loaded_versions(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "models": {"priority": 1, "sentiment": 1},
    }


def test_a_missing_model_is_downloaded_from_s3(tmp_path, monkeypatch):
    save_models(tmp_path / "saved", version=2)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-2")
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-east-2"}
        )
        for name in ["priority", "sentiment"]:
            for filename in ["metadata.json", "model.joblib"]:
                path = tmp_path / "saved" / name / "v2" / filename
                s3.upload_file(str(path), BUCKET, s3_key(name, 2, filename))
        settings = Settings(
            models_dir=tmp_path / "cache",
            s3_bucket=BUCKET,
            priority_model_version=2,
            sentiment_model_version=2,
            _env_file=None,
        )

        with client_for(monkeypatch, settings) as client:
            assert client.get("/health").json()["models"] == {"priority": 2, "sentiment": 2}
    assert (tmp_path / "cache" / "priority" / "v2" / "model.joblib").exists()


def test_the_service_wont_start_without_its_models(tmp_path):
    settings = Settings(models_dir=tmp_path, s3_bucket=None, _env_file=None)

    with pytest.raises(RuntimeError, match="No priority v1"):
        service.load_models(settings)


def test_a_model_saved_by_another_scikit_learn_is_refused(tmp_path):
    save_models(tmp_path)
    path = tmp_path / "priority" / "v1" / "metadata.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"sklearn_version": "0.1.0"}))

    with pytest.raises(RuntimeError, match="saved with scikit-learn 0.1.0"):
        service.load_models(Settings(models_dir=tmp_path, _env_file=None))
