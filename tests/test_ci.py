"""The GitHub Actions workflow must check what the Mac checks, the same way.

These tests read .github/workflows/ci.yml. GitHub runs it.
"""

import re

import pytest
import yaml
from sqlalchemy import make_url

from app.config import PROJECT_ROOT

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def jobs(workflow):
    return workflow["jobs"]


def commands(job):
    return [step["run"] for step in job["steps"] if "run" in step]


def uv_versions(job):
    return [step["with"]["version"] for step in job["steps"] if "setup-uv" in step.get("uses", "")]


def test_every_push_is_checked(workflow):
    # YAML reads a bare `on:` key as True.
    assert "push" in workflow[True]
    assert workflow[True]["push"] is None  # No branch filter, so every branch.


def test_lint_runs_ruff(jobs):
    assert "uv run ruff check ." in commands(jobs["lint"])


def test_the_tests_run(jobs):
    assert "uv run pytest" in commands(jobs["test"])


def test_every_job_installs_exactly_what_uv_lock_says(jobs):
    for job in jobs.values():
        if uv_versions(job):
            assert "uv sync --locked" in commands(job)


def test_ci_uses_the_same_uv_as_the_image(jobs):
    dockerfile = (PROJECT_ROOT / "infra" / "api.Dockerfile").read_text()
    [image_uv] = re.findall(r"ghcr\.io/astral-sh/uv:(\S+) ", dockerfile)
    for job in jobs.values():
        assert all(version == image_uv for version in uv_versions(job))


def test_the_tests_run_on_the_same_postgres_and_redis_as_compose(jobs):
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())["services"]
    services = jobs["test"]["services"]
    assert services["postgres"]["image"] == compose["postgres"]["image"]
    assert services["redis"]["image"] == compose["redis"]["image"]


def test_the_tests_use_the_service_containers_database(jobs):
    postgres = jobs["test"]["services"]["postgres"]["env"]
    url = make_url(jobs["test"]["env"]["TEST_DATABASE_URL"])
    assert (url.host, url.port) == ("localhost", 5432)
    assert url.database == postgres["POSTGRES_DB"]
    assert url.database.endswith("_test")


def test_no_jwt_secret_is_written_in_the_workflow(jobs):
    # It's made fresh on each run, so the repo holds no key anyone could sign tokens with.
    assert "JWT_SECRET_KEY" not in jobs["test"]["env"]
    assert any("openssl rand" in command for command in commands(jobs["test"]))
