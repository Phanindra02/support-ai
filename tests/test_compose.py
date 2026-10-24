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
    secret_settings = {
        "JWT_SECRET_KEY",
        "TWILIO_AUTH_TOKEN",
        "N8N_WEBHOOK_SECRET",
        "LAMBDA_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
    }
    assert not secret_settings & api_environment.keys()
    assert services["api"]["env_file"] == ".env"


# The EC2 server's changes, in infra/aws/compose.ec2.yml.


@pytest.fixture(scope="module")
def ec2_compose():
    return yaml.load(EC2_COMPOSE_FILE.read_text(), Loader=ComposeLoader)


@pytest.fixture(scope="module")
def ec2_services(ec2_compose):
    return ec2_compose["services"]


def test_on_ec2_the_database_changes_and_caddy_is_added(ec2_services):
    assert set(ec2_services) == {"api", "migrate", "postgres", "caddy"}


def test_on_ec2_the_api_and_its_migrations_load_their_secrets_from_parameter_store(ec2_services):
    for name in ["api", "migrate"]:
        environment = ec2_services[name]["environment"]
        assert environment["PARAMETER_STORE_PATH"] == "/support-ai"
        # An environment variable would win over Parameter Store, so the container's URL from
        # docker-compose.yml is dropped, not replaced.
        assert environment["DATABASE_URL"] == RESET


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


# Caddy, which serves the API over HTTPS on EC2 (infra/aws/Caddyfile).

CADDYFILE = PROJECT_ROOT / "infra" / "aws" / "Caddyfile"
DOMAIN = "support.mysolospark.com"


@pytest.fixture(scope="module")
def caddy(ec2_services):
    return ec2_services["caddy"]


@pytest.fixture(scope="module")
def caddyfile():
    # The Caddyfile's lines, without comments or blank lines.
    lines = (line.strip() for line in CADDYFILE.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_caddy_gets_a_certificate_for_the_domain(caddyfile):
    # A domain name turns on automatic HTTPS. A port like ":80", "http://", or an IP address
    # would serve plain HTTP instead.
    assert caddyfile[0] == f"{DOMAIN} {{"


def test_caddy_passes_requests_to_the_apis_port_on_the_server(services, caddy, caddyfile):
    # The port docker-compose.yml publishes on 127.0.0.1, reached from the server's own network.
    [published] = services["api"]["ports"]
    api_address = published.rsplit(":", 1)[0]
    assert f"reverse_proxy {api_address}" in caddyfile
    assert caddy["network_mode"] == "host"


def test_on_ec2_forwarded_headers_are_still_only_trusted_from_the_gateway(ec2_services):
    # Caddy's connections to 127.0.0.1:8000 arrive from the gateway, so docker-compose.yml's
    # setting stays as it is. Widening it would let other containers choose the client's IP.
    assert "FORWARDED_ALLOW_IPS" not in ec2_services["api"]["environment"]


def test_caddy_replaces_the_forwarded_headers_clients_send():
    # trusted_proxies would make Caddy keep a client's X-Forwarded-For, and the API would then
    # trust whatever address the client made up.
    assert "trusted_proxies" not in CADDYFILE.read_text()


def test_caddy_reads_the_caddyfile_from_the_repo(caddy):
    # Relative to the project root, where docker-compose.yml is, like every path compose reads.
    assert "./infra/aws/Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]
    assert CADDYFILE.exists()


def test_caddy_keeps_its_certificates_when_the_container_is_replaced(ec2_compose, caddy):
    # Let's Encrypt allows 5 certificates a week for the same name.
    assert "caddy-data:/data" in caddy["volumes"]
    assert "caddy-data" in ec2_compose["volumes"]


def test_caddy_stays_on_its_major_version(caddy):
    # Bug and security fixes on each pull, but never a surprise move to Caddy 3.
    assert caddy["image"] == "caddy:2"
    assert caddy["restart"] == "unless-stopped"
