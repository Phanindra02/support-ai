"""The Twilio webhook. These tests play Twilio: they sign requests with a made-up auth token,
the same way Twilio does, so no Twilio account or network is needed."""

import pytest
from twilio.request_validator import RequestValidator

from app.config import settings

AUTH_TOKEN = "made-up-twilio-auth-token-for-tests"
URL = "http://testserver/webhooks/twilio"  # The URL the test client calls.
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response />'


@pytest.fixture(autouse=True)
def twilio_auth_token(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)


@pytest.fixture
def client(anonymous_client):
    """Twilio doesn't log in. Its signature proves who it is instead."""
    return anonymous_client


def sign(form, url=URL, token=AUTH_TOKEN):
    """The X-Twilio-Signature header Twilio would send with this form."""
    return RequestValidator(token).compute_signature(url, form)


def whatsapp_form(**fields):
    """The form Twilio posts for one WhatsApp message (most of its fields, not all)."""
    return {
        "MessageSid": "SM" + "0" * 31 + "1",
        "AccountSid": "AC" + "0" * 32,
        "From": "whatsapp:+16145550123",
        "To": "whatsapp:+14155238886",
        "Body": "Where is my order?",
        "ProfileName": "Jane Doe",
        "WaId": "16145550123",
        "NumMedia": "0",
        **fields,
    }


def post_from_twilio(client, form, signature=None, headers=None):
    signature = sign(form) if signature is None else signature
    headers = {"X-Twilio-Signature": signature, **(headers or {})}
    return client.post("/webhooks/twilio", data=form, headers=headers)


# The signature check


def test_a_request_signed_by_twilio_gets_twiml_back(client):
    response = post_from_twilio(client, whatsapp_form())
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/xml"
    assert response.text == EMPTY_TWIML


def test_a_request_without_a_signature_gets_a_403(client):
    response = client.post("/webhooks/twilio", data=whatsapp_form())
    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid Twilio signature"}


@pytest.mark.parametrize(
    "signature",
    [
        "",
        "not-a-signature",
        sign(whatsapp_form(), token="someone-elses-auth-token"),
        sign(whatsapp_form(), url="https://evil.example.com/webhooks/twilio"),
        "é".encode("latin-1") * 28,  # Not ASCII, which a header can still carry.
    ],
)
def test_a_wrong_signature_gets_a_403(client, signature):
    response = post_from_twilio(client, whatsapp_form(), signature=signature)
    assert response.status_code == 403


def test_changing_any_field_after_signing_gets_a_403(client):
    form = whatsapp_form()
    signature = sign(form)
    tampered = {**form, "Body": "Refund me $1,000"}
    assert post_from_twilio(client, tampered, signature=signature).status_code == 403


def test_adding_a_field_after_signing_gets_a_403(client):
    form = whatsapp_form()
    signature = sign(form)
    extra = {**form, "Admin": "true"}
    assert post_from_twilio(client, extra, signature=signature).status_code == 403


def test_a_file_upload_gets_a_403_not_a_500(client):
    form = whatsapp_form()
    response = client.post(
        "/webhooks/twilio",
        data=form,
        files={"Body": ("note.txt", b"hello")},
        headers={"X-Twilio-Signature": sign(form)},
    )
    assert response.status_code == 403


def test_a_host_header_with_a_bad_port_does_not_cause_a_500(client):
    # Parsing a URL like http://testserver:abc/ raises an error. Starlette ignores a Host
    # header with a bad port and uses the server's own address, which is what was signed.
    form = whatsapp_form()
    response = post_from_twilio(client, form, headers={"Host": "testserver:abc"})
    assert response.status_code == 200


@pytest.mark.parametrize("token", [None, ""])
def test_every_request_is_turned_away_until_the_auth_token_is_set(client, monkeypatch, token):
    monkeypatch.setattr(settings, "twilio_auth_token", token)
    response = post_from_twilio(client, whatsapp_form())
    assert response.status_code == 503
    assert "TWILIO_AUTH_TOKEN" in response.json()["detail"]


def test_the_webhook_skips_the_ip_rate_limit(client, monkeypatch):
    # Every customer's message comes from Twilio's servers, so a per-IP limit would hold
    # them all back together.
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    statuses = [post_from_twilio(client, whatsapp_form()).status_code for _ in range(5)]
    assert statuses == [200] * 5
