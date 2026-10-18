"""Settings come from environment variables or .env, and .env.example lists every one."""

import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings
from app.database import engine

URL = "postgresql+psycopg://user:secret@db.example.com:5432/support"
REDIS_URL = "redis://cache.example.com:6379/0"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"


@pytest.fixture
def no_settings_in_environment(monkeypatch):
    """Remove every setting from the environment, so only the file under test counts."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_reads_settings_from_environment_variables(no_settings_in_environment, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    # _env_file=None skips the real .env, so only the environment counts.
    settings = Settings(_env_file=None)
    assert settings.database_url == URL
    assert settings.redis_url == REDIS_URL


def test_reads_settings_from_an_env_file(no_settings_in_environment, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={URL}\nREDIS_URL={REDIS_URL}\n")
    settings = Settings(_env_file=env_file)
    assert settings.database_url == URL
    assert settings.redis_url == REDIS_URL


def test_an_environment_variable_wins_over_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATABASE_URL=postgresql+psycopg://from-the-file/support\nREDIS_URL={REDIS_URL}\n"
    )
    monkeypatch.setenv("DATABASE_URL", URL)
    assert Settings(_env_file=env_file).database_url == URL


@pytest.mark.parametrize("missing", ["database_url", "redis_url"])
def test_a_missing_required_setting_is_an_error(no_settings_in_environment, monkeypatch, missing):
    required = {"DATABASE_URL": URL, "REDIS_URL": REDIS_URL}
    del required[missing.upper()]
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=missing):
        Settings(_env_file=None)


def test_env_example_lists_every_setting():
    lines = ENV_EXAMPLE.read_text().splitlines()
    names = {line.split("=", 1)[0] for line in lines if line.strip() and not line.startswith("#")}
    assert names == {name.upper() for name in Settings.model_fields}


def test_env_example_values_load(no_settings_in_environment):
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")


def test_the_app_connects_with_psycopg_3():
    assert engine.url.drivername == "postgresql+psycopg"
