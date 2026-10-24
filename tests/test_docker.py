"""The API's Dockerfile and .dockerignore must keep the image safe and buildable.

Building the image takes Docker and a minute, so these tests read the two files instead.
CI builds the real image.
"""

import re
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT

DOCKERFILE = PROJECT_ROOT / "infra" / "api.Dockerfile"
MODEL_DOCKERFILE = PROJECT_ROOT / "infra" / "model.Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"


def instructions(path):
    """A file's lines, without comments or blank lines, with continued lines joined."""
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


@pytest.fixture(scope="module")
def dockerfile():
    return instructions(DOCKERFILE)


@pytest.fixture(scope="module")
def dockerignore():
    return instructions(DOCKERIGNORE)


def final_stage(dockerfile):
    """The instructions after the last FROM, which make the image that runs."""
    last_from = max(i for i, line in enumerate(dockerfile) if line.startswith("FROM "))
    return dockerfile[last_from:]


def test_only_listed_files_reach_the_build(dockerignore):
    # Leaving everything out first means a new file stays out until someone lists it.
    assert dockerignore[0] == "*"


def test_env_never_reaches_the_build(dockerignore):
    allowed = [line[1:] for line in dockerignore if line.startswith("!")]
    assert not [name for name in allowed if ".env" in name]


def test_everything_the_image_copies_reaches_the_build(dockerfile, dockerignore):
    allowed = {line[1:].rstrip("/") for line in dockerignore if line.startswith("!")}
    for line in dockerfile:
        if line.startswith("COPY ") and "--from=" not in line:
            *sources, _destination = [word for word in line.split()[1:] if word[:2] != "--"]
            for source in sources:
                assert source.rstrip("/") in allowed, f"{source} is missing from .dockerignore"


def test_the_image_runs_as_a_user_other_than_root(dockerfile):
    [user] = [line for line in final_stage(dockerfile) if line.startswith("USER ")]
    assert user.split()[1] not in {"0", "root"}


def test_the_image_uses_the_projects_python():
    python_version = (PROJECT_ROOT / ".python-version").read_text().strip()
    images = re.findall(r"^FROM (\S+)", DOCKERFILE.read_text(), re.MULTILINE)
    assert images == [f"python:{python_version}-slim"] * 2


def test_the_image_installs_exactly_what_uv_lock_says(dockerfile):
    # --locked stops the build if uv.lock is out of date. --no-default-groups leaves out every
    # group in pyproject.toml, such as pytest (dev) and pypdf (lambda), not only dev.
    [sync] = [line for line in dockerfile if "uv sync" in line]
    assert "--locked" in sync.split()
    assert "--no-default-groups" in sync.split()


def test_the_code_is_readable_whichever_machine_builds_it(dockerfile):
    # COPY keeps the build machine's file permissions unless --chmod sets them.
    for line in final_stage(dockerfile):
        if line.startswith("COPY ") and "--from=" not in line:
            assert "--chmod=u=rwX,go=rX" in line.split()


def test_the_image_has_the_rds_certificates_where_the_driver_looks(dockerfile):
    # libpq, the driver under psycopg, reads PGSSLROOTCERT to check the database's certificate.
    final = final_stage(dockerfile)
    [workdir] = [line.split()[1] for line in final if line.startswith("WORKDIR ")]
    [copy] = [line for line in final if "rds-ca-us-east-2.pem" in line and line.startswith("COPY ")]
    source, destination = copy.split()[-2:]
    assert (PROJECT_ROOT / source).exists()
    assert (
        f"ENV PGSSLROOTCERT={workdir}/{destination.removeprefix('./')}{Path(source).name}" in final
    )


@pytest.fixture(scope="module")
def model_dockerfile():
    return instructions(MODEL_DOCKERFILE)


def test_the_model_image_copies_only_files_that_reach_the_build(model_dockerfile, dockerignore):
    test_everything_the_image_copies_reaches_the_build(model_dockerfile, dockerignore)


def test_the_model_image_installs_only_the_service_group(model_dockerfile):
    [sync] = [line for line in model_dockerfile if "uv sync" in line]
    assert "--locked" in sync.split()
    assert "--only-group service" in sync


def test_the_model_image_runs_as_a_user_other_than_root(model_dockerfile):
    test_the_image_runs_as_a_user_other_than_root(model_dockerfile)


def test_the_model_image_holds_every_project_module_the_service_imports(model_dockerfile):
    # Each ml module the image copies may import only ml modules it also copies.
    [copy] = [line for line in model_dockerfile if line.startswith("COPY") and "ml/" in line]
    copied = {Path(word).stem for word in copy.split() if word.startswith("ml/")}
    for module in copied:
        source = (PROJECT_ROOT / "ml" / f"{module}.py").read_text()
        imported = set(re.findall(r"^from ml\.(\w+) import", source, re.MULTILINE))
        assert imported <= copied, f"ml/{module}.py imports {imported - copied}"
        assert not re.search(r"^(from|import) (app|pandas)\b", source, re.MULTILINE)
