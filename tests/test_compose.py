"""docker-compose.yml must start the whole stack, wired together and closed to the outside.

These tests read the file. Starting the stack takes Docker and half a minute, and was
checked by hand (see docs/stages/04-docker-ci.md).
"""

import ipaddress
import re

import pytest
import yaml
from sqlalchemy import make_url

from app.config import PROJECT_ROOT, Settings

COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
EC2_COMPOSE_FILE = PROJECT_ROOT / "infra" / "aws" / "compose.ec2.yml"

# What compose's !reset tag reads as here. Plain YAML doesn't know the tag.
RESET = "!reset"


class ComposeLoader(yaml.SafeLoader):
    """safe_load, plus the !reset tag compose uses to drop a value from an earlier file."""


ComposeLoader.add_constructor(RESET, lambda loader, node: RESET)


@pytest.fixture(scope="module")
def compose():
    # safe_load understands the "<<: *api" lines that copy one block into another.
    return yaml.safe_load(COMPOSE_FILE.read_text())


@pytest.fixture(scope="module")
def services(compose):
    return compose["services"]


@pytest.fixture(scope="module")
def api_environment(services):
    return services["api"]["environment"]


def test_one_command_starts_the_whole_stack(services):
    assert {"api", "migrate", "postgres", "redis", "n8n"} <= services.keys()


def test_the_api_and_its_migrations_build_the_same_image(services):
    for name in ["api", "migrate"]:
        assert services[name]["build"]["dockerfile"] == "infra/api.Dockerfile"
        # support-ai-api unless API_IMAGE names another, like CI's image in ECR on EC2.
        assert services[name]["image"] == "${API_IMAGE:-support-ai-api}"
    assert (PROJECT_ROOT / "infra" / "api.Dockerfile").exists()


def test_the_api_waits_for_the_migrations_to_finish(services):
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    waits_for = services["api"]["depends_on"]
    assert waits_for["migrate"]["condition"] == "service_completed_successfully"
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_the_api_talks_to_the_other_containers(services, api_environment):
    database = make_url(api_environment["DATABASE_URL"])
    assert database.drivername == "postgresql+psycopg"
    assert database.host == "postgres"
    assert database.database == services["postgres"]["environment"]["POSTGRES_DB"]
    assert database.username == services["postgres"]["environment"]["POSTGRES_USER"]
    assert api_environment["REDIS_URL"].startswith("redis://redis:")


def test_alerts_go_to_the_n8n_container_on_the_workflows_path(api_environment):
    # Only the host changes. The path is where the workflow in infra/n8n/ listens.
    default_url = Settings.model_fields["n8n_webhook_url"].default
    assert api_environment["N8N_WEBHOOK_URL"] == default_url.replace("localhost", "n8n")


def test_only_the_api_and_the_n8n_editor_are_published(services):
    published = {name for name, service in services.items() if "ports" in service}
    assert published == {"api", "n8n"}


def test_published_ports_only_listen_on_the_mac_itself(services):
    for service in services.values():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:")


def test_proxy_headers_are_only_trusted_from_the_networks_gateway(compose, api_environment):
    # "*" would let any client choose the IP address that the rate limit counts.
    [network] = compose["networks"]["default"]["ipam"]["config"]
    assert api_environment["FORWARDED_ALLOW_IPS"] == network["gateway"]
    assert ipaddress.ip_address(network["gateway"]) in ipaddress.ip_network(network["subnet"])


def test_n8n_is_pinned_to_an_exact_version(services):
    # n8n upgrades its database when a new version starts, so it should never change unseen.
    assert re.fullmatch(r".+:\d+\.\d+\.\d+", services["n8n"]["image"])


def test_data_survives_replacing_a_container(services):
    assert "postgres-data:/var/lib/postgresql/data" in services["postgres"]["volumes"]
    assert "n8n-data:/home/node/.n8n" in services["n8n"]["volumes"]


def test_no_secret_is_written_in_the_file(services, api_environment):
    # Secrets come from .env, through env_file or ${...}.
    assert "${POSTGRES_PASSWORD" in services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert make_url(api_environment["DATABASE_URL"]).password.startswith("${POSTGRES_PASSWORD")
    secret_settings = {"JWT_SECRET_KEY", "TWILIO_AUTH_TOKEN", "N8N_WEBHOOK_SECRET"}
    assert not secret_settings & api_environment.keys()
    assert services["api"]["env_file"] == ".env"


# The EC2 server's changes, in infra/aws/compose.ec2.yml.


@pytest.fixture(scope="module")
def ec2_services():
    return yaml.load(EC2_COMPOSE_FILE.read_text(), Loader=ComposeLoader)["services"]


def test_on_ec2_only_the_database_changes(ec2_services):
    assert set(ec2_services) == {"api", "migrate", "postgres"}


def test_on_ec2_the_api_and_its_migrations_use_the_rds_url_from_env(ec2_services):
    # Required (:?), so a server without the URL stops instead of starting without a database.
    for name in ["api", "migrate"]:
        assert ec2_services[name]["environment"]["DATABASE_URL"].startswith("${DATABASE_URL:?")


def test_on_ec2_the_postgres_container_never_starts(ec2_services):
    assert ec2_services["postgres"]["profiles"]
    # The migrations would otherwise wait for a container that never starts.
    assert ec2_services["migrate"]["depends_on"] == RESET


def test_the_servers_env_turns_the_ec2_file_on():
    line = "COMPOSE_FILE=docker-compose.yml:infra/aws/compose.ec2.yml"
    assert line in (PROJECT_ROOT / "infra" / "aws" / "README.md").read_text()
    assert f"# {line}" in (PROJECT_ROOT / ".env.example").read_text()
    for path in line.split("=", 1)[1].split(":"):
        assert (PROJECT_ROOT / path).exists()
