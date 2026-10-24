# syntax=docker/dockerfile:1

# The image for the model service (ml/service.py). Build it from the project root:
#   docker build -f infra/model.Dockerfile -t support-ai-model .
#
# Built the same way as infra/api.Dockerfile, in two stages, but it holds only the service's
# few files and dependencies: no API code, database driver, or LangChain.

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.2 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# --only-group service installs that group's packages alone, without the project's own
# dependencies, which are the API's.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --only-group service


FROM python:3.14-slim

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --chmod=u=rwX,go=rX ml/__init__.py ml/registry.py ml/service.py ml/text.py ./ml/

# Models downloaded from S3 go here. The app user owns it, since the code is read-only.
RUN mkdir /models && chown app:app /models
ENV MODELS_DIR=/models \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER 10001

EXPOSE 8000

CMD ["uvicorn", "ml.service:app", "--host", "0.0.0.0", "--port", "8000"]
