"""The Twilio webhook. These tests play Twilio: they sign requests with a made-up auth token,
the same way Twilio does, so no Twilio account or network is needed."""

import json
import time
from threading import Thread
from uuid import uuid4
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.config import settings
from app.models import Customer, Message, MessageSender, Ticket, TicketPriority, TicketStatus
from app.routers.tickets import STATS_KEY
from app.routers.webhooks import MAX_REPLY_LENGTH, confirmation

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


@pytest.fixture
def agent_client(test_app, make_agent, auth_header):
    """An agent working the tickets, next to Twilio's client, which isn't logged in."""
    agent = make_agent(name="Ada Lovelace", email="ada@example.com")
    return TestClient(test_app, headers=auth_header(agent))


def sign(form, url=URL, token=AUTH_TOKEN):
    """The X-Twilio-Signature header Twilio would send with this form."""
    return RequestValidator(token).compute_signature(url, form)


def new_message_sid():
    """An id like the ones Twilio gives each message: SM and 32 hex digits."""
    return "SM" + uuid4().hex


def whatsapp_form(**fields):
    """The form Twilio posts for one WhatsApp message (most of its fields, not all).

    Each call is a new message, with its own MessageSid, unless one is passed in.
    """
    return {
        "MessageSid": new_message_sid(),
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
    _closed, shared = all_tickets(session_factory)
    assert shared["subject"] == "Opened by the other request"
    assert shared["messages"] == [(MessageSender.customer, "Second message")]


# The confirmation reply


def replies(response):
    """The messages a TwiML response tells Twilio to send back."""
    return [message.text for message in ElementTree.fromstring(response.text).iter("Message")]


def test_a_message_that_opens_a_ticket_gets_a_confirmation(client):
    response = send(client, "Where is my order?")
    expected = (
        "Thanks, we got your message. Your ticket number is 1, "
        "and someone from our team will reply here soon."
    )
    assert replies(response) == [expected]


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


# Repeat deliveries. Twilio can send the same message more than once, with the same MessageSid.


def deliver(client, form):
    """Deliver this exact form, as Twilio does each time it sends a message again."""
    response = post_from_twilio(client, form)
    assert response.status_code == 200, response.text
    return response


def test_a_repeat_delivery_stores_nothing_and_gets_the_same_reply(client, session_factory):
    form = whatsapp_form(Body="Where is my order?")
    first = deliver(client, form)
    repeat = deliver(client, form)

    # The confirmation again, since Twilio only repeats a delivery when it didn't use the reply.
    assert repeat.text == first.text
    assert "Your ticket number is 1," in replies(repeat)[0]
    [ticket] = all_tickets(session_factory)
    assert ticket["messages"] == [(MessageSender.customer, "Where is my order?")]


def test_a_repeat_of_a_later_message_gets_no_reply_either(client, session_factory):
    send(client, "Where is my order?")
    form = whatsapp_form(Body="It's order 1234.")
    deliver(client, form)

    assert deliver(client, form).text == EMPTY_TWIML
    [ticket] = all_tickets(session_factory)
    assert len(ticket["messages"]) == 2


def test_a_repeat_after_the_ticket_closed_opens_no_new_ticket(client, session_factory):
    form = whatsapp_form()
    deliver(client, form)
    with session_factory() as db:
        db.get(Ticket, 1).status = TicketStatus.closed
        db.commit()

    # Without the check, the repeat would look like a new message and open ticket 2.
    repeat = deliver(client, form)

    assert "Your ticket number is 1," in replies(repeat)[0]
    [ticket] = all_tickets(session_factory)
    assert ticket["status"] == TicketStatus.closed


def test_a_repeat_leaves_a_pending_ticket_pending(client, session_factory):
    send(client)
    form = whatsapp_form(Body="Here's the photo you asked for.")
    deliver(client, form)
    with session_factory() as db:
        db.get(Ticket, 1).status = TicketStatus.pending  # The team answered the photo.
        db.commit()

    deliver(client, form)

    [ticket] = all_tickets(session_factory)
    assert ticket["status"] == TicketStatus.pending


def test_the_same_text_in_two_messages_is_kept_twice(client, session_factory):
    # Repeats are found by Twilio's id, not by text. A customer can send "Hello?" twice.
    send(client, "Hello?")
    send(client, "Hello?")
    [ticket] = all_tickets(session_factory)
    assert [body for _, body in ticket["messages"]] == ["Hello?", "Hello?"]


def test_only_whatsapp_messages_have_a_twilio_id(
    client, agent_client, session_factory, ticket_data
):
    form = whatsapp_form()
    deliver(client, form)
    # Any number of messages can have none, despite the unique constraint.
    for _ in range(2):
        assert agent_client.post("/tickets", json=ticket_data).status_code == 201

    with session_factory() as db:
        ids = db.scalars(select(Message.twilio_sid).order_by(Message.id)).all()
    assert ids == [form["MessageSid"], None, None]


def test_a_repeat_that_arrives_while_the_first_is_saving_waits_for_it(client, session_factory):
    send(client, "First message")
    with session_factory() as db:
        db.get(Ticket, 1).status = TicketStatus.closed
        db.commit()
    form = whatsapp_form(Body="New problem")

    # The first delivery of form is half done: it holds the lock on the customer's row, and
    # has opened ticket 2 with the message, but hasn't committed yet.
    with session_factory() as first_delivery:
        customer = first_delivery.scalar(select(Customer).with_for_update())
        ticket = Ticket(customer=customer, subject="New problem")
        first_delivery.add(
            Message(
                ticket=ticket,
                sender=MessageSender.customer,
                body="New problem",
                twilio_sid=form["MessageSid"],
            )
        )
        first_delivery.flush()
        results = []
        thread = Thread(target=lambda: results.append(deliver(client, form)))
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive(), "The repeat should wait for the lock"

        first_delivery.commit()  # Releases the lock.
        thread.join(timeout=5)

    # The repeat found the stored message, instead of saving it again and failing with a 500.
    assert results and "Your ticket number is 2," in replies(results[0])[0]
    _closed, new = all_tickets(session_factory)
    assert new["messages"] == [(MessageSender.customer, "New problem")]


@pytest.mark.parametrize("sid", ["", "SM" + "0" * 40, "SM\x00abc", "SM-1234"])
def test_a_bad_message_id_gets_a_422_not_a_500(client, session_factory, sid):
    response = post_from_twilio(client, whatsapp_form(MessageSid=sid))
    assert response.status_code == 422
    assert all_tickets(session_factory) == []


def test_a_message_without_an_id_gets_a_422(client, session_factory):
    form = whatsapp_form()
    del form["MessageSid"]
    assert post_from_twilio(client, form).status_code == 422
    assert all_tickets(session_factory) == []


# From WhatsApp to Slack, with Twilio and n8n both faked


def test_a_whatsapp_ticket_marked_urgent_reaches_slack(client, agent_client, n8n, monkeypatch):
    monkeypatch.setattr(settings, "n8n_webhook_secret", "made-up-n8n-secret-for-tests")

    reply = send(client, "My card was charged twice\nPlease refund one.", ProfileName="Jane Doe")
    assert "Your ticket number is 1," in replies(reply)[0]
    assert n8n.requests == []  # A new ticket is medium priority, so no alert yet.

    response = agent_client.patch("/tickets/1", json={"priority": "urgent"})
    assert response.status_code == 200, response.text

    [request] = n8n.requests
    assert request.headers["X-Webhook-Secret"] == "made-up-n8n-secret-for-tests"
    alert = json.loads(request.content)
    assert alert["subject"] == "My card was charged twice"
    assert (alert["customer_name"], alert["marked_urgent_by"]) == ("Jane Doe", "Ada Lovelace")

    # The customer writes again. The ticket is already urgent, so Slack hears nothing new.
    assert send(client, "Any news?").text == EMPTY_TWIML
    assert len(n8n.requests) == 1


# The bot answers first


@pytest.fixture
def refund_docs(save_chunks):
    """Help docs that say how long a refund takes, pointing the way every question does."""
    save_chunks([("refunds.pdf", "Refunds take 5 days.", (1.0, 0.0))])


def test_the_bot_answers_from_the_docs_and_names_them(
    client, use_bot, save_chunks, session_factory
):
    save_chunks(
        [
            ("refunds.pdf", "Refunds take 5 days.", (1.0, 0.0)),
            ("payments.pdf", "Refunds go to the card you paid with.", (1.0, 0.1)),
        ]
    )
    llm = use_bot(reply="Refunds take 5 days, back to the card you paid with.")

    response = send(client, "How long does a refund take?")

    assert replies(response) == [
        "Refunds take 5 days, back to the card you paid with.\n\nSources: refunds.pdf, payments.pdf"
    ]
    assert llm.prompts[0][1].content.endswith("Customer's question: How long does a refund take?")
    # An answered question needs no one from the team, so nothing is stored.
    assert all_tickets(session_factory) == []


def test_one_source_is_named_as_one(client, use_bot, refund_docs):
    use_bot(reply="Refunds take 5 days.")

    response = send(client, "How long does a refund take?")

    assert replies(response) == ["Refunds take 5 days.\n\nSource: refunds.pdf"]


def test_when_the_bot_doesnt_know_a_ticket_opens_and_someone_will_follow_up(
    client, use_bot, refund_docs, session_factory
):
    use_bot(reply="I don't know.")

    response = send(client, "Can I change my delivery address?")

    [ticket] = all_tickets(session_factory)
    assert ticket["subject"] == "Can I change my delivery address?"
    assert ticket["messages"] == [(MessageSender.customer, "Can I change my delivery address?")]
    assert replies(response) == [confirmation(ticket["id"])]


def test_a_customer_talking_to_the_team_doesnt_get_the_bot(client, use_bot, refund_docs):
    llm = use_bot(reply="I don't know.")
    send(client, "Can I change my delivery address?")  # Opens a ticket.

    # The bot would know this one, but a person is already looking after this customer.
    response = send(client, "And how long does a refund take?")

    assert response.text == EMPTY_TWIML  # It joined the ticket, which already has a reply.
    assert len(llm.prompts) == 1


def test_when_openai_fails_the_message_goes_to_the_team(
    client, use_bot, refund_docs, session_factory, caplog
):
    use_bot(timed_out=True)

    response = send(client, "How long does a refund take?")

    [ticket] = all_tickets(session_factory)
    assert replies(response) == [confirmation(ticket["id"])]
    assert "The bot couldn't reach OpenAI" in caplog.text


def test_a_message_without_text_goes_straight_to_the_team(
    client, use_bot, refund_docs, session_factory
):
    llm = use_bot()

    send(client, "", NumMedia="1")

    assert llm.prompts == []
    assert len(all_tickets(session_factory)) == 1


def test_a_long_answer_is_cut_to_fit_one_whatsapp_message(client, use_bot, refund_docs):
    use_bot(reply="Refunds take 5 days. " * 100)

    [reply] = replies(send(client, "How long does a refund take?"))

    assert len(reply) == MAX_REPLY_LENGTH == 1600
    assert reply.endswith("\u2026")
