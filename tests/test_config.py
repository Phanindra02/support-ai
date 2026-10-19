"""Settings come from environment variables or .env, and .env.example lists every one."""

import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings
from app.database import engine

URL = "postgresql+psycopg://user:secret@db.example.com:5432/support"
REDIS_URL = "redis://cache.example.com:6379/0"
JWT_SECRET_KEY = "k" * 64
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# The settings with no default, which the app can't start without.
REQUIRED = {"DATABASE_URL": URL, "REDIS_URL": REDIS_URL, "JWT_SECRET_KEY": JWT_SECRET_KEY}


def env_lines(settings):
    return "".join(f"{name}={value}\n" for name, value in settings.items())


@pytest.fixture
def no_settings_in_environment(monkeypatch):
    """Remove every setting from the environment, so only the file under test counts."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_reads_settings_from_environment_variables(no_settings_in_environment, monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    # _env_file=None skips the real .env, so only the environment counts.
    settings = Settings(_env_file=None)
    assert settings.database_url == URL
    assert settings.redis_url == REDIS_URL
    assert settings.jwt_secret_key == JWT_SECRET_KEY


def test_reads_settings_from_an_env_file(no_settings_in_environment, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(env_lines(REQUIRED))
    settings = Settings(_env_file=env_file)
    assert settings.database_url == URL
    assert settings.redis_url == REDIS_URL
    assert settings.jwt_secret_key == JWT_SECRET_KEY


def test_an_environment_variable_wins_over_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        env_lines({**REQUIRED, "DATABASE_URL": "postgresql+psycopg://from-the-file/support"})
    )
    monkeypatch.setenv("DATABASE_URL", URL)
    assert Settings(_env_file=env_file).database_url == URL


@pytest.mark.parametrize("missing", [name.lower() for name in REQUIRED])
def test_a_missing_required_setting_is_an_error(no_settings_in_environment, monkeypatch, missing):
    for name, value in REQUIRED.items():
        if name != missing.upper():
            monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=missing):
        Settings(_env_file=None)


def test_a_short_jwt_secret_is_an_error(no_settings_in_environment, monkeypatch):
    for name, value in {**REQUIRED, "JWT_SECRET_KEY": "secret"}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match="jwt_secret_key"):
        Settings(_env_file=None)


def test_env_example_lists_every_setting():
    lines = ENV_EXAMPLE.read_text().splitlines()
    names = {line.split("=", 1)[0] for line in lines if line.strip() and not line.startswith("#")}
    assert names == {name.upper() for name in Settings.model_fields}


def test_env_example_values_load(no_settings_in_environment, monkeypatch):
    # The real JWT secret has to come from somewhere else, such as .env.
    monkeypatch.setenv("JWT_SECRET_KEY", JWT_SECRET_KEY)
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")


def test_the_app_will_not_start_with_the_jwt_secret_from_env_example(no_settings_in_environment):
    # Everyone can read .env.example on GitHub, so a key from it could sign anyone's token.
    with pytest.raises(ValidationError, match="jwt_secret_key"):
        Settings(_env_file=ENV_EXAMPLE)


def test_the_app_connects_with_psycopg_3():
    assert engine.url.drivername == "postgresql+psycopg"
