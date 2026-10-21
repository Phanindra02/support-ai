"""The n8n workflow in infra/n8n/ must accept what app/alerts.py sends.

The API and the workflow change in different places, one in Python and one in n8n's editor,
so nothing else would notice if they drifted apart until an urgent ticket went missing.
"""

import json
import re

import pytest

from app.alerts import SECRET_HEADER
from app.config import PROJECT_ROOT, Settings
from app.schemas import UrgentAlert

WORKFLOW = PROJECT_ROOT / "infra" / "n8n" / "urgent-ticket.json"
# The fields a customer writes, through WhatsApp or the ticket form.
CUSTOMER_TEXT = ["subject", "customer_name"]


@pytest.fixture(scope="module")
def workflow():
    return json.loads(WORKFLOW.read_text())


def node(workflow, node_type):
    [found] = [n for n in workflow["nodes"] if n["type"] == node_type]
    return found


@pytest.fixture(scope="module")
def webhook(workflow):
    return node(workflow, "n8n-nodes-base.webhook")


@pytest.fixture(scope="module")
def slack_text(workflow):
    return node(workflow, "n8n-nodes-base.slack")["parameters"]["text"]


def test_the_webhook_listens_where_the_api_posts(webhook):
    default_url = Settings.model_fields["n8n_webhook_url"].default
    assert webhook["parameters"]["httpMethod"] == "POST"
    assert default_url.endswith("/webhook/" + webhook["parameters"]["path"])


def test_the_webhook_checks_the_shared_secret(webhook):
    # The header's name and value live in the n8n credential, which the API's header must match.
    assert webhook["parameters"]["authentication"] == "headerAuth"
    assert webhook["credentials"]["httpHeaderAuth"]["name"] == "Support AI webhook secret"
    assert SECRET_HEADER == "X-Webhook-Secret"


def test_the_webhook_feeds_the_slack_node(workflow, webhook):
    [[link]] = workflow["connections"][webhook["name"]]["main"]
    assert link["node"] == node(workflow, "n8n-nodes-base.slack")["name"]


def test_the_message_only_uses_fields_the_api_sends(slack_text):
    used = set(re.findall(r"\$json\.body\.(\w+)", slack_text))
    assert used, "The Slack message should show the ticket"
    assert used <= set(UrgentAlert.model_fields)


@pytest.mark.parametrize("field", CUSTOMER_TEXT)
def test_customer_text_is_escaped_for_slack(slack_text, field):
    # Slack reads <...> as a link or mention, so <!channel> in a subject would notify everyone.
    escaped = (
        f"$json.body.{field}.replaceAll('&', '&amp;')"
        ".replaceAll('<', '&lt;').replaceAll('>', '&gt;')"
    )
    assert escaped in slack_text
    assert slack_text.count(f"$json.body.{field}") == 1  # No unescaped copy elsewhere.


def test_the_file_holds_no_secrets(workflow):
    # An export names each credential, and n8n keeps the values in its own database.
    for n in workflow["nodes"]:
        for credential in n.get("credentials", {}).values():
            assert set(credential) == {"id", "name"}
    assert "xoxb-" not in WORKFLOW.read_text()
