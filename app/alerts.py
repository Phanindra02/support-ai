"""Alerts to the support team's Slack channel, sent through n8n.

The API posts an urgent ticket to an n8n webhook, and the workflow in infra/n8n/ posts it to
Slack. n8n owns the Slack part, so changing the message or the channel needs no code change.

Like the cache, an alert is never allowed to break the API. If n8n is slow or down, the
ticket is still saved, and a warning is logged.
"""

import logging

import httpx

from app.config import settings
from app.schemas import UrgentAlert

logger = logging.getLogger(__name__)

# A ticket update shouldn't leave a thread waiting long on n8n.
TIMEOUT_SECONDS = 5

http_client = httpx.Client(timeout=TIMEOUT_SECONDS)


def get_http_client() -> httpx.Client:
    """The HTTP client for calling n8n, as a FastAPI dependency, so tests can swap in a fake."""
    return http_client


def send_urgent_alert(client: httpx.Client, alert: UrgentAlert) -> None:
    """Post an urgent ticket to the n8n webhook. Runs after the response has been sent."""
    try:
        response = client.post(settings.n8n_webhook_url, json=alert.model_dump(mode="json"))
        response.raise_for_status()
    except httpx.HTTPError as error:
        # Nothing retries yet. Stage 6 adds a job queue, which could.
        logger.warning("Couldn't send the urgent-ticket alert to n8n: %s", error)
