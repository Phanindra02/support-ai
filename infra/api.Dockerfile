# syntax=docker/dockerfile:1

# The image for the API, its migrations, and the worker, which docker-compose.yml runs with a
# different command for each. Build it from the project root, which is the build context:
#   docker build -f infra/api.Dockerfile -t support-ai-api .
#
# Two stages: the first installs the dependencies with uv, and the second copies only the
# installed result. uv, its cache, and the build steps stay out of the image that runs.

FROM python:3.14-slim AS builder

# uv's own image holds just the uv program. Pinned to the version used on the Mac.
COPY --from=ghcr.io/astral-sh/uv:0.13.2 /uv /bin/uv

# Compile .pyc files now, so the app doesn't on every start. Copy packages out of uv's cache
# rather than linking to it, because the cache is only mounted during the build. And use the
# image's Python, instead of letting uv download another.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# The dependencies get a layer of their own, before any code. They change far less often
# than the code, so Docker reuses this layer, and a code change rebuilds in seconds.
# --locked fails the build if uv.lock is out of date. --no-default-groups leaves out the
# dependency groups: pytest and the other tools only needed for development, and pypdf, which
# only the Lambda uses. --no-dev alone would still install the lambda group.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups


FROM python:3.14-slim

# The app runs as this user, not root, so a bug in it can't change the image's files or
# take over the container.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app

WORKDIR /app

# The code is owned by root, so the app user can read it but not change it. --chmod sets
# the same permissions whichever machine builds the image: folders 755 and files 644.
# Without it, COPY keeps the build machine's own, which may not let the app user read them.
COPY --from=builder /app/.venv ./.venv
COPY --chmod=u=rwX,go=rX alembic.ini ./
COPY --chmod=u=rwX,go=rX alembic ./alembic
COPY --chmod=u=rwX,go=rX app ./app
COPY --chmod=u=rwX,go=rX worker ./worker
COPY --chmod=u=rwX,go=rX scripts/__init__.py scripts/create_agent.py scripts/export_predictions.py scripts/failed_jobs.py ./scripts/

# Amazon's root certificates for RDS in us-east-2. The RDS URL asks for sslmode=verify-full,
# so the database driver (libpq) checks that the server's certificate was signed by one of
# these and names the host it connected to. PGSSLROOTCERT tells libpq where they are. Postgres
# without TLS, like the compose container on the Mac, never looks at them.
COPY --chmod=u=rwX,go=rX infra/aws/rds-ca-us-east-2.pem ./certs/
ENV PGSSLROOTCERT=/app/certs/rds-ca-us-east-2.pem

# The virtual environment's programs (uvicorn, alembic, python) come first on the PATH.
# PYTHONUNBUFFERED sends log lines out at once, so `docker compose logs` shows them live.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# By number rather than name, so Kubernetes (Stage 8) can check that it isn't root.
USER 10001

EXPOSE 8000

# 0.0.0.0, so the app listens on the container's network, not just inside it. The exec form
# (a JSON list) makes uvicorn process 1, so it gets Docker's stop signal and shuts down
# cleanly, finishing requests already in progress.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
