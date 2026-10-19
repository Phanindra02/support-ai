"""The Twilio webhook. These tests play Twilio: they sign requests with a made-up auth token,
the same way Twilio does, so no Twilio account or network is needed."""

import time
from threading import Thread
from xml.etree import ElementTree

import pytest
from sqlalchemy import select
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.config import settings
from app.models import Customer, MessageSender, Ticket, TicketPriority, TicketStatus
from app.routers.tickets import STATS_KEY

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
    assert response.text.startswith('<?xml version="1.0" encoding="UTF-8"?><Response>')


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


# Messages become tickets


def all_tickets(session_factory):
    """Every ticket, oldest first, with its customer and messages, as plain values."""
    with session_factory() as db:
        tickets = db.scalars(select(Ticket).order_by(Ticket.id)).all()
        return [
            {
                "id": ticket.id,
                "customer": (ticket.customer.name, ticket.customer.phone),
                "subject": ticket.subject,
                "status": ticket.status,
                "priority": ticket.priority,
                "messages": [
                    (m.sender, m.body)
                    for m in sorted(ticket.messages, key=lambda m: (m.created_at, m.id))
                ],
            }
            for ticket in tickets
        ]


def send(client, body="Where is my order?", **fields):
    response = post_from_twilio(client, whatsapp_form(Body=body, **fields))
    assert response.status_code == 200, response.text
    return response


def test_a_first_message_opens_a_ticket_for_a_new_customer(client, session_factory):
    send(client, "Where is my order?\nIt was due on Monday.")

    assert all_tickets(session_factory) == [
        {
            "id": 1,
            "customer": ("Jane Doe", "+16145550123"),
            "subject": "Where is my order?",
            "status": TicketStatus.open,
            "priority": TicketPriority.medium,
            "messages": [
                (MessageSender.customer, "Where is my order?\nIt was due on Monday.")
            ],
        }
    ]


def test_more_messages_join_the_open_ticket_in_order(client, session_factory):
    send(client, "Where is my order?")
    send(client, "It's order 1234.")
    send(client, "Thanks!")

    [ticket] = all_tickets(session_factory)
    assert [body for _, body in ticket["messages"]] == [
        "Where is my order?",
        "It's order 1234.",
        "Thanks!",
    ]


def test_a_reply_to_a_pending_ticket_makes_it_open_again(client, session_factory):
    send(client)
    with session_factory() as db:
        db.get(Ticket, 1).status = TicketStatus.pending  # The team replied and is waiting.
        db.commit()

    send(client, "Here's the photo you asked for.")

    [ticket] = all_tickets(session_factory)
    assert ticket["status"] == TicketStatus.open
    assert len(ticket["messages"]) == 2


def test_a_message_after_the_ticket_closed_opens_a_new_one(client, session_factory):
    send(client, "Where is my order?")
    with session_factory() as db:
        db.get(Ticket, 1).status = TicketStatus.closed
        db.commit()

    send(client, "Now I need a refund.")

    first, second = all_tickets(session_factory)
    assert first["status"] == TicketStatus.closed
    assert len(first["messages"]) == 1
    assert (second["subject"], second["status"]) == ("Now I need a refund.", TicketStatus.open)
    assert first["customer"] == second["customer"]


def test_a_message_joins_the_newest_of_several_open_tickets(client, session_factory):
    send(client, "First problem")
    with session_factory() as db:
        customer = db.scalar(select(Customer))
        db.add(Ticket(customer=customer, subject="Opened by an agent"))
        db.commit()

    send(client, "Following up")

    first, newest = all_tickets(session_factory)
    assert len(first["messages"]) == 1
    assert newest["messages"][-1] == (MessageSender.customer, "Following up")


def test_a_customer_an_agent_already_added_keeps_their_name(client, session_factory):
    with session_factory() as db:
        db.add(Customer(name="Jane Doe-Smith", phone="+16145550123"))
        db.commit()

    send(client, "Hello", ProfileName="JD")

    [ticket] = all_tickets(session_factory)
    assert ticket["customer"] == ("Jane Doe-Smith", "+16145550123")


def test_a_customer_with_no_profile_name_goes_by_their_number(client, session_factory):
    form = whatsapp_form()
    del form["ProfileName"]
    post_from_twilio(client, form)
    assert all_tickets(session_factory)[0]["customer"] == ("+16145550123", "+16145550123")


def test_a_long_profile_name_is_cut_to_fit(client, session_factory):
    send(client, ProfileName="J" * 150)
    name, _ = all_tickets(session_factory)[0]["customer"]
    assert name == "J" * 100


def test_a_long_first_line_is_cut_for_the_subject(client, session_factory):
    send(client, "x" * 200)
    [ticket] = all_tickets(session_factory)
    assert ticket["subject"] == "x" * 79 + "…"
    assert ticket["messages"][0][1] == "x" * 200  # The message itself is kept whole.


def test_a_message_without_text_still_shows_in_the_conversation(client, session_factory):
    send(client, "", NumMedia="1", MediaUrl0="https://api.twilio.com/photo.jpg")
    [ticket] = all_tickets(session_factory)
    assert ticket["subject"] == "WhatsApp message"
    assert "No text" in ticket["messages"][0][1]


def test_nul_characters_are_removed_rather_than_refused(client, session_factory):
    send(client, "Hel\x00lo", ProfileName="Ja\x00ne")
    [ticket] = all_tickets(session_factory)
    assert ticket["customer"][0] == "Jane"
    assert ticket["messages"][0][1] == "Hello"


@pytest.mark.parametrize("sender", ["+16145550123", "whatsapp:16145550123", "messenger:+1614"])
def test_a_sender_that_is_not_a_whatsapp_number_gets_a_422(client, session_factory, sender):
    response = post_from_twilio(client, whatsapp_form(From=sender))
    assert response.status_code == 422
    assert all_tickets(session_factory) == []


def test_a_message_clears_the_cached_stats(client, redis_client):
    redis_client.set(STATS_KEY, "old counts")
    send(client)
    assert redis_client.get(STATS_KEY) is None


def test_messages_sent_at_once_by_one_customer_share_a_ticket(client, session_factory):
    send(client, "First message")
    with session_factory() as db:
        db.get(Ticket, 1).status = TicketStatus.closed
        db.commit()

    # Another webhook request for the same customer is half done: it holds the lock on the
    # customer's row, and is about to open a new ticket.
    with session_factory() as other_request:
        customer = other_request.scalar(select(Customer).with_for_update())
        results = []
        thread = Thread(target=lambda: results.append(send(client, "Second message")))
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive(), "The webhook should wait for the lock"

        other_request.add(Ticket(customer=customer, subject="Opened by the other request"))
        other_request.commit()  # Releases the lock.
        thread.join(timeout=5)

    assert results and results[0].status_code == 200
    # The waiting request found the ticket the other one opened, instead of opening a third.
    closed, shared = all_tickets(session_factory)
    assert shared["subject"] == "Opened by the other request"
    assert shared["messages"] == [(MessageSender.customer, "Second message")]


# The confirmation reply


def replies(response):
    """The messages a TwiML response tells Twilio to send back."""
    return [message.text for message in ElementTree.fromstring(response.text).iter("Message")]


def test_a_message_that_opens_a_ticket_gets_a_confirmation(client):
    response = send(client, "Where is my order?")
    assert replies(response) == [
        "Thanks, we got your message. Your ticket number is 1, "
        "and someone from our team will reply here soon."
    ]


def test_later_messages_on_the_same_ticket_get_no_reply(client):
    send(client, "Where is my order?")
    response = send(client, "It's order 1234.")
    assert response.text == EMPTY_TWIML


def test_a_new_ticket_after_a_closed_one_gets_its_own_number(client, session_factory):
    send(client, "Where is my order?")
    with session_factory() as db:
        db.get(Ticket, 1).status = TicketStatus.closed
        db.commit()

    response = send(client, "Now I need a refund.")

    assert "Your ticket number is 2," in replies(response)[0]


def test_text_in_the_reply_is_escaped_for_xml(client):
    # TwiML is XML, so the library escapes <, >, and &. Only numbers go in this reply today,
    # but this keeps it safe if names or subjects are added later.
    reply = MessagingResponse()
    reply.message("<Redirect>https://evil.example.com</Redirect> & more")
    assert "<Redirect>" not in str(reply)
    assert [m.text for m in ElementTree.fromstring(str(reply)).iter("Message")] == [
        "<Redirect>https://evil.example.com</Redirect> & more"
    ]
