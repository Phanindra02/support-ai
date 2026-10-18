from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The project root, so .env is found no matter which folder a command runs from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Settings read from environment variables, or from the git-ignored .env file.

    A real environment variable wins over the same name in .env, so Docker and CI can
    change a setting without editing any file. Names match case-insensitively, so the
    DATABASE_URL variable fills database_url.
    """

    # extra="ignore" skips variables in .env that aren't settings here.
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # No default, so a missing value stops the app at startup with a clear error,
    # instead of quietly connecting to the wrong database.
    database_url: str

    # Only the tests use this. They empty this database, so it must never be the real one.
    test_database_url: str | None = None

    # Redis, for the cache. The number at the end picks one of Redis's numbered databases.
    redis_url: str

    # Only the tests use this. They empty this Redis database, so it must differ from REDIS_URL.
    test_redis_url: str | None = None

    # How many requests each IP address may make in any 60 seconds.
    rate_limit_per_minute: int = Field(default=60, ge=1)


settings = Settings()
