"""The model service: predicts a ticket's priority and a customer's mood.

A separate app from the API, with its own image, so loading and running the models can't slow
the API down, and a new model version ships by restarting this service alone. When it starts,
it loads the versions its settings name from MODELS_DIR, downloading them from S3 first if
they aren't there.

uv run uvicorn ml.service:app --port 8001
"""

from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Annotated

import boto3
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sklearn.pipeline import Pipeline

from ml.registry import MODELS_DIR, PROJECT_ROOT, download, load, model_folder
from ml.text import clean_text


class Settings(BaseSettings):
    """The service's own few settings. It reads none of the API's, so it holds none of its secrets."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    models_dir: Path = MODELS_DIR
    # Where a model missing from models_dir is downloaded from (ml/save.py uploads it there).
    s3_bucket: str | None = None
    aws_region: str = "us-east-2"
    # Changing a version and restarting is how a new model ships, or an old one comes back.
    priority_model_version: int = 1
    sentiment_model_version: int = 1


@cache
def get_settings() -> Settings:
    return Settings()


def load_models(settings: Settings) -> dict[str, tuple[Pipeline, int]]:
    """Each model and its version, downloaded first if it isn't on disk yet."""
    models = {}
    for name, version in [
        ("priority", settings.priority_model_version),
        ("sentiment", settings.sentiment_model_version),
    ]:
        if not (model_folder(settings.models_dir, name, version) / "model.joblib").exists():
            if not settings.s3_bucket:
                raise RuntimeError(f"No {name} v{version} in {settings.models_dir}, and no S3_BUCKET")
            s3 = boto3.client("s3", region_name=settings.aws_region)
            download(s3, settings.s3_bucket, settings.models_dir, name, version)
        models[name] = (load(settings.models_dir, name, version), version)
    return models


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loaded once at start, not per request. A model that can't load stops the start, so a
    # broken service never takes traffic.
    app.state.models = load_models(get_settings())
    yield


app = FastAPI(title="Support AI model service", lifespan=lifespan)


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=10_000)


class Prediction(BaseModel):
    label: str
    # Each label's probability, adding up to 1, so a caller can set its own bar, such as
    # "very negative" meaning a negative score above 0.8.
    scores: dict[str, float]
    model: str
    version: int


def get_models(request: Request) -> dict[str, tuple[Pipeline, int]]:
    return request.app.state.models


ModelsDep = Annotated[dict[str, tuple[Pipeline, int]], Depends(get_models)]


def predict(models: dict[str, tuple[Pipeline, int]], name: str, text: str) -> Prediction:
    model, version = models[name]
    probabilities = model.predict_proba([clean_text(text)])[0]
    scores = {str(label): round(float(p), 4) for label, p in zip(model.classes_, probabilities)}
    return Prediction(label=max(scores, key=scores.get), scores=scores, model=name, version=version)


@app.post("/predict/priority")
def predict_priority(body: PredictRequest, models: ModelsDep) -> Prediction:
    return predict(models, "priority", body.text)


@app.post("/predict/sentiment")
def predict_sentiment(body: PredictRequest, models: ModelsDep) -> Prediction:
    return predict(models, "sentiment", body.text)


@app.get("/health")
def health(models: ModelsDep) -> dict:
    """Answers only once the models are loaded, so it's safe to send requests."""
    return {"status": "ok", "models": {name: version for name, (_, version) in models.items()}}
