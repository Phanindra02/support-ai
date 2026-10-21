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


def image_steps(jobs):
    return [step for step in jobs["image"]["steps"] if "build-push-action" in step.get("uses", "")]


def test_an_image_is_only_built_after_lint_and_the_tests_pass(jobs):
    assert set(jobs["image"]["needs"]) == {"lint", "test"}


def test_the_image_is_built_on_arm(jobs):
    # The Mac and AWS Graviton are ARM too.
    assert jobs["image"]["runs-on"].endswith("-arm")


def test_only_pushes_to_main_publish_the_image(jobs):
    for step in jobs["image"]["steps"]:
        publishes = step.get("with", {}).get("push") is True or "login-action" in step.get("uses", "")
        if publishes:
            assert step["if"] == "github.ref == 'refs/heads/main'"


def test_only_the_image_job_may_write_packages(workflow, jobs):
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in jobs.items():
        if name != "image":
            assert "permissions" not in job


def test_no_build_args_go_into_the_image(jobs):
    # The repo and its image are public. Anything passed as a build arg is stored in the image.
    for step in image_steps(jobs):
        assert "build-args" not in step["with"]
        assert "secrets" not in step["with"]
    dockerfile = (PROJECT_ROOT / "infra" / "api.Dockerfile").read_text()
    assert not re.search(r"^\s*ARG\b", dockerfile, re.MULTILINE)


def test_the_published_image_is_built_from_the_dockerfile(jobs):
    for step in image_steps(jobs):
        assert step["with"]["file"] == "infra/api.Dockerfile"
        assert step["with"]["context"] == "."
