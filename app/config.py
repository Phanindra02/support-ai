from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from pydantic import Field
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# The project root, so .env is found no matter which folder a command runs from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The app can't start without its settings, so a slow or unreachable Parameter Store should
# fail the start within seconds, not boto3's default of minutes.
PARAMETER_STORE_CONFIG = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"mode": "standard", "total_max_attempts": 3},
)


class ParameterStoreSource(PydanticBaseSettingsSource):
    """Settings from AWS Systems Manager Parameter Store, under PARAMETER_STORE_PATH.

    /support-ai/DATABASE_URL fills database_url, the same as the DATABASE_URL variable
    would. SecureString values are decrypted. Only used where PARAMETER_STORE_PATH is set,
    which is on EC2, so the Mac and CI never call AWS for settings.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Required by the base class, but unused: __call__ reads every parameter at once.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        # current_state holds what the environment and .env already set.
        path = self.current_state.get("parameter_store_path")
        if not path:
            return {}
        default_region = self.settings_cls.model_fields["aws_region"].default
        region = self.current_state.get("aws_region", default_region)
        ssm = boto3.client("ssm", region_name=region, config=PARAMETER_STORE_CONFIG)
        values = {}
        # Only the parameters directly under the path, not deeper ones. One call returns at
        # most 10, so the paginator asks for the rest.
        for page in ssm.get_paginator("get_parameters_by_path").paginate(
            Path=path, WithDecryption=True
        ):
            for parameter in page["Parameters"]:
                name = parameter["Name"].rsplit("/", 1)[-1].lower()
                # Like .env, a name that isn't a setting is skipped.
                if name in self.settings_cls.model_fields:
                    values[name] = parameter["Value"]
        return values


class Settings(BaseSettings):
    """Settings read from environment variables, the git-ignored .env file, or Parameter Store.

    The first place that sets a value wins, in this order: a real environment variable, then
    .env, then Parameter Store (only where PARAMETER_STORE_PATH is set), then the default here.
    So Docker and CI can change a setting without editing any file. Names match
    case-insensitively, so the DATABASE_URL variable fills database_url.
    """

    # extra="ignore" skips variables in .env that aren't settings here. env_ignore_empty
    # treats NAME= as not set, so an empty line in .env doesn't hide the value in Parameter
    # Store, and leaves a setting with a default at its default.
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", extra="ignore", env_ignore_empty=True
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The default order, with Parameter Store added after the environment and .env.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            ParameterStoreSource(settings_cls),
            file_secret_settings,
        )

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

    # Signs agents' login tokens (JWTs). Anyone who has it can make a token for any agent.
    # HS256 needs a key at least as long as its 32-byte output to be as strong as it can be.
    jwt_secret_key: str = Field(min_length=32)

    # How long a login token works before the agent has to log in again.
    access_token_expire_minutes: int = Field(default=30, ge=1)

    # Twilio signs every webhook request with this, so the app can check it came from Twilio.
    # Until it's set, the WhatsApp webhook turns every request away.
    twilio_auth_token: str | None = None

    # The n8n webhook that posts urgent tickets to Slack (infra/n8n/urgent-ticket.json).
    n8n_webhook_url: str = "http://localhost:5678/webhook/urgent-ticket"

    # Sent with every call to that webhook, which n8n checks, so only the API can trigger it.
    # No alerts are sent until it's set.
    n8n_webhook_secret: str | None = None

    # The private S3 bucket for help docs (infra/aws/README.md). Uploads are turned away
    # until it's set. AWS keys aren't settings: boto3 finds them by itself, in ~/.aws on the
    # Mac and from the instance's IAM role on EC2.
    s3_bucket: str | None = None

    # The Lambda sends this in the X-Webhook-Secret header when it has pulled out a PDF's
    # text, so only it can mark documents processed. Until it's set, that webhook turns every
    # request away.
    lambda_webhook_secret: str | None = None

    # OpenAI, for both the embeddings and the bot's answers, so there's one provider to pay
    # and one key to guard. Nothing calls OpenAI until it's set.
    openai_api_key: str | None = None

    # The model service (ml/service.py), which the worker asks for each new ticket's priority.
    # Until it's set, no prediction is queued and new tickets stay at medium.
    model_service_url: str | None = None

    # Where the project's AWS resources are.
    aws_region: str = "us-east-2"

    # The Parameter Store path to load settings from, such as /support-ai, which holds the
    # secrets on AWS (infra/aws/README.md). Empty on the Mac and in CI, which skip it.
    parameter_store_path: str | None = None


settings = Settings()  # type: ignore[call-arg]  # Values are loaded from the sources above.
