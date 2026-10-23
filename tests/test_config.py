"""Settings come from environment variables or .env, and .env.example lists every one."""

import re

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings
from app.database import engine

URL = "postgresql+psycopg://user:secret@db.example.com:5432/support"
REDIS_URL = "redis://cache.example.com:6379/0"
JWT_SECRET_KEY = "k" * 64
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
COMPOSE_FILES = [PROJECT_ROOT / "docker-compose.yml", PROJECT_ROOT / "infra/aws/compose.ec2.yml"]

# The settings with no default, which the app can't start without.
REQUIRED = {"DATABASE_URL": URL, "REDIS_URL": REDIS_URL, "JWT_SECRET_KEY": JWT_SECRET_KEY}

# Values that would let someone sign tokens, fake a webhook, or open the database.
SECRETS = {
    "JWT_SECRET_KEY",
    "TWILIO_AUTH_TOKEN",
    "N8N_WEBHOOK_SECRET",
    "LAMBDA_WEBHOOK_SECRET",
    "POSTGRES_PASSWORD",
}


def env_lines(settings):
    return "".join(f"{name}={value}\n" for name, value in settings.items())


def env_example_values():
    lines = ENV_EXAMPLE.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line.strip() and not line.startswith("#"))


@pytest.fixture
def no_settings_in_environment(monkeypatch):
    """Remove every setting from the environment, so only the file under test counts."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_reads_settings_from_environment_variables(no_settings_in_environment, monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    # _env_file=None skips the real .env, so only the environment counts.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_url == URL
    assert settings.redis_url == REDIS_URL
    assert settings.jwt_secret_key == JWT_SECRET_KEY


def test_reads_settings_from_an_env_file(no_settings_in_environment, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(env_lines(REQUIRED))
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.database_url == URL
    assert settings.redis_url == REDIS_URL
    assert settings.jwt_secret_key == JWT_SECRET_KEY


def test_an_environment_variable_wins_over_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        env_lines({**REQUIRED, "DATABASE_URL": "postgresql+psycopg://from-the-file/support"})
    )
    monkeypatch.setenv("DATABASE_URL", URL)
    assert Settings(_env_file=env_file).database_url == URL  # type: ignore[call-arg]


@pytest.mark.parametrize("missing", [name.lower() for name in REQUIRED])
def test_a_missing_required_setting_is_an_error(no_settings_in_environment, monkeypatch, missing):
    for name, value in REQUIRED.items():
        if name != missing.upper():
            monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=missing):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_a_short_jwt_secret_is_an_error(no_settings_in_environment, monkeypatch):
    for name, value in {**REQUIRED, "JWT_SECRET_KEY": "secret"}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match="jwt_secret_key"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_env_example_lists_every_setting():
    # The app's settings, plus the ${NAME} values docker compose fills in from .env.
    compose_text = "".join(path.read_text() for path in COMPOSE_FILES)
    compose_names = set(re.findall(r"\$\{(\w+)", compose_text))
    expected = {name.upper() for name in Settings.model_fields} | compose_names
    assert env_example_values().keys() == expected


def test_env_example_leaves_every_secret_empty():
    # Everyone can read .env.example on GitHub.
    values = env_example_values()
    assert {name: values[name] for name in SECRETS} == dict.fromkeys(SECRETS, "")


def test_env_is_never_committed():
    assert ".env" in (PROJECT_ROOT / ".gitignore").read_text().splitlines()


def test_settings_are_only_read_in_app_config():
    # Then .env.example, checked against Settings above, names every setting there is.
    for folder in ["app", "scripts", "alembic"]:
        for path in (PROJECT_ROOT / folder).rglob("*.py"):
            if path != PROJECT_ROOT / "app" / "config.py":
                assert not re.search(r"os\.environ|getenv|load_dotenv", path.read_text()), path


def test_env_example_values_load(no_settings_in_environment, monkeypatch):
    # The real JWT secret has to come from somewhere else, such as .env.
    monkeypatch.setenv("JWT_SECRET_KEY", JWT_SECRET_KEY)
    settings = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")


def test_the_app_will_not_start_with_the_jwt_secret_from_env_example(no_settings_in_environment):
    # Everyone can read .env.example on GitHub, so a key from it could sign anyone's token.
    with pytest.raises(ValidationError, match="jwt_secret_key"):
        Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]


def test_the_app_connects_with_psycopg_3():
    assert engine.url.drivername == "postgresql+psycopg"


# Parameter Store, which holds the secrets on EC2. moto's mock_aws stands in for AWS.

PATH = "/support-ai"


@pytest.fixture
def parameter_store(no_settings_in_environment, monkeypatch):
    """A fake Parameter Store, and PARAMETER_STORE_PATH pointing at it."""
    with mock_aws():
        monkeypatch.setenv("PARAMETER_STORE_PATH", PATH)
        ssm = boto3.client("ssm", region_name="us-east-2")

        def put(name, value):
            ssm.put_parameter(Name=f"{PATH}/{name}", Value=value, Type="SecureString")

        yield put


def test_settings_missing_from_the_environment_come_from_parameter_store(parameter_store):
    for name, value in REQUIRED.items():
        parameter_store(name, value)
    parameter_store("TWILIO_AUTH_TOKEN", "twilio-token")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    # SecureString values arrive decrypted.
    assert settings.database_url == URL
    assert settings.jwt_secret_key == JWT_SECRET_KEY
    assert settings.twilio_auth_token == "twilio-token"


def test_the_environment_and_env_win_over_parameter_store(parameter_store, monkeypatch, tmp_path):
    for name in REQUIRED:
        parameter_store(name, "x" * 40)
    monkeypatch.setenv("DATABASE_URL", URL)
    env_file = tmp_path / ".env"
    env_file.write_text(f"REDIS_URL={REDIS_URL}\n")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.database_url == URL
    assert settings.redis_url == REDIS_URL
    assert settings.jwt_secret_key == "x" * 40


def test_an_empty_line_in_env_does_not_hide_parameter_store(parameter_store, tmp_path):
    # .env.example leaves every secret as NAME=, and the server's .env starts as a copy of it.
    for name, value in REQUIRED.items():
        parameter_store(name, value)
    env_file = tmp_path / ".env"
    env_file.write_text("JWT_SECRET_KEY=\nDATABASE_URL=\n")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.jwt_secret_key == JWT_SECRET_KEY
    assert settings.database_url == URL


def test_only_settings_directly_under_the_path_are_loaded(parameter_store):
    for name, value in REQUIRED.items():
        parameter_store(name, value)
    # More than the 10 one call returns, so the rest must come from the next page.
    for number in range(10):
        parameter_store(f"NOT_A_SETTING_{number}", "ignored")
    parameter_store("N8N_WEBHOOK_SECRET", "n8n-secret")
    parameter_store("deeper/TWILIO_AUTH_TOKEN", "not-this-one")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.n8n_webhook_secret == "n8n-secret"
    assert settings.twilio_auth_token is None


def test_a_setting_missing_everywhere_is_still_an_error(parameter_store):
    parameter_store("DATABASE_URL", URL)
    parameter_store("REDIS_URL", REDIS_URL)
    with pytest.raises(ValidationError, match="jwt_secret_key"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_without_a_path_parameter_store_is_never_asked(no_settings_in_environment, monkeypatch):
    # The Mac and CI have no PARAMETER_STORE_PATH, and must start without AWS.
    def no_aws(*args, **kwargs):
        raise AssertionError("Parameter Store was asked")

    monkeypatch.setattr(boto3, "client", no_aws)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    assert Settings(_env_file=None).database_url == URL  # type: ignore[call-arg]
