"""The Twilio webhook: customers' WhatsApp messages arrive here.

Twilio POSTs each incoming WhatsApp message to POST /webhooks/twilio as a form. The response
is TwiML, an XML format Twilio reads, and any message in it is sent back to the customer.

The bot (app/bot.py) gets each message first, and answers it from the help docs if it can.
When it can't, the message opens a ticket for the team, as it did before there was a bot.

This router skips the per-IP rate limit. Every customer's message arrives from Twilio's
servers, so a limit per IP would hold back all customers together. The signature check
below means only Twilio can call it.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from openai import OpenAIError
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.bot import Bot, BotAnswer, BotDep, answer_with_cache
from app.cache import RedisDep, clear_cache
from app.config import settings
from app.database import SessionDep
from app.models import Customer, Message, MessageSender, Ticket, TicketStatus
from app.routers.tickets import STATS_KEY, get_or_create_customer
from app.schemas import ErrorResponse, WhatsAppMessage

logger = logging.getLogger(__name__)

# The most text Twilio sends in one WhatsApp message. A longer reply would fail to send.
MAX_REPLY_LENGTH = 1600

# Tickets a human is working on. A customer with one is talking to the team, not the bot.
ACTIVE_STATUSES = (TicketStatus.open, TicketStatus.pending)

# A new ticket's subject is the first line of the customer's message, cut to this length.
SUBJECT_LENGTH = 80
# Stored as the message when the customer sent no text, so the conversation still shows it.
NO_TEXT = "(No text. The customer sent something else, such as a photo.)"


def confirmation(ticket_id: int) -> str:
    """The reply a customer gets when their message opens a ticket."""
    return (
        f"Thanks, we got your message. Your ticket number is {ticket_id}, "
        "and someone from our team will reply here soon."
    )


async def verify_twilio_signature(request: Request) -> None:
    """Stop any request that Twilio didn't sign, with a 403.

    Twilio signs each request with an HMAC-SHA1 of the full URL it called and every form
    field, keyed with the account's auth token, and sends it in X-Twilio-Signature. Only
    Twilio and this app know the token, so only Twilio can make a signature that matches.
    """
    if not settings.twilio_auth_token:
        raise HTTPException(
            status_code=503, detail="Twilio isn't set up: TWILIO_AUTH_TOKEN is empty"
        )

    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(settings.twilio_auth_token)
    # The URL must be the public one Twilio called. Behind ngrok on the Mac, or Caddy on EC2,
    # the host comes from the Host header the proxy passes on, and uvicorn takes the https
    # scheme from the proxy's X-Forwarded-Proto header. It only trusts that header from the
    # addresses in FORWARDED_ALLOW_IPS: 127.0.0.1 by default, and the Docker network's gateway
    # in docker-compose.yml, where both proxies' connections arrive.
    # Twilio sends plain text fields. A file upload can't be signed, and would crash the
    # check, so it fails it instead.
    valid = all(isinstance(value, str) for _, value in form.multi_items()) and (
        validator.validate(str(request.url), form, signature)
    )
    if not valid:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
    dependencies=[Depends(verify_twilio_signature)],
    responses={
        403: {"model": ErrorResponse, "description": "Not signed by Twilio"},
        503: {"model": ErrorResponse, "description": "TWILIO_AUTH_TOKEN isn't set"},
    },
)


def twiml(reply: MessagingResponse) -> Response:
    return Response(content=str(reply), media_type="application/xml")


def subject_from(body: str) -> str:
    """A subject for a ticket opened on WhatsApp: the first line of the message."""
    first_line = body.strip().split("\n", 1)[0].strip()
    if not first_line:
        return "WhatsApp message"
    if len(first_line) > SUBJECT_LENGTH:
        return first_line[: SUBJECT_LENGTH - 1] + "\u2026"  # An ellipsis: …
    return first_line


def find_open_ticket(db: Session, customer: Customer) -> Ticket | None:
    """The customer's newest ticket that isn't closed, if they have one."""
    return db.scalar(
        select(Ticket)
        .where(
            Ticket.customer_id == customer.id,
            Ticket.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(1)
    )


def has_active_ticket(db: Session, phone: str) -> bool:
    """Whether the customer with this number has a ticket the team is working on."""
    ticket_id = db.scalar(
        select(Ticket.id)
        .join(Ticket.customer)
        .where(Customer.phone == phone, Ticket.status.in_(ACTIVE_STATUSES))
        .limit(1)
    )
    return ticket_id is not None


def ask_bot(
    db: Session, redis: Redis, bot: Bot | None, message: WhatsAppMessage
) -> BotAnswer | None:
    """The bot's answer to the message, or None when the team should take it instead.

    The team takes it when the bot isn't set up, the message has no text, the customer is
    already talking to the team, OpenAI fails, or the help docs don't hold the answer.
    """
    question = message.body.strip()
    if bot is None or not question or has_active_ticket(db, message.phone):
        return None
    try:
        answer = answer_with_cache(db, redis, bot, question)
    except OpenAIError:
        # The customer still gets a reply, from a person rather than the bot.
        logger.exception("The bot couldn't reach OpenAI, so the message goes to the team")
        return None
    return answer if answer.known else None


def bot_reply(answer: BotAnswer) -> str:
    """The bot's answer, then the help docs it came from, cut to fit one WhatsApp message."""
    label = "Source" if len(answer.sources) == 1 else "Sources"
    text = f"{answer.text}\n\n{label}: {', '.join(answer.sources)}"
    if len(text) > MAX_REPLY_LENGTH:
        return text[: MAX_REPLY_LENGTH - 1] + "\u2026"
    return text


def opened_its_ticket(db: Session, message: Message) -> bool:
    """Whether a stored message is the one that opened its ticket.

    Every way a ticket opens, POST /tickets or this webhook, saves its first message with it,
    so the message that opened a ticket is the ticket's first.
    """
    first_id = db.scalar(
        select(func.min(Message.id)).where(Message.ticket_id == message.ticket_id)
    )
    return first_id == message.id


def add_to_ticket(db: Session, message: WhatsAppMessage) -> tuple[Ticket, bool]:
    """Add a customer's message to their open ticket, or open a new ticket with it.

    Also says whether the message opened the ticket, which decides the reply. A message
    that's already stored changes nothing, and gets the same answer as the first time.
    """
    # A customer without a WhatsApp profile name goes by their number until an agent knows more.
    name = message.profile_name.strip()[:100] or message.phone
    customer = get_or_create_customer(db, name, message.phone)

    # Lock the customer's row until this request commits. WhatsApp users often send a few short
    # messages in a row, and Twilio delivers them at the same moment. Without the lock, two
    # requests could both find no open ticket, and open two. With it, the second waits, then
    # finds the ticket the first one opened.
    db.refresh(customer, with_for_update=True)

    # Twilio sometimes delivers a message twice, such as when it gave up waiting for the
    # first response. Checked after the lock, so a repeat that arrives while the first is
    # still being saved waits for it, then finds it, instead of saving it again.
    stored = db.scalar(select(Message).where(Message.twilio_sid == message.message_sid))
    if stored is not None:
        return stored.ticket, opened_its_ticket(db, stored)

    ticket = find_open_ticket(db, customer)
    is_new = ticket is None
    if ticket is None:
        ticket = Ticket(customer=customer, subject=subject_from(message.body))
        db.add(ticket)
    elif ticket.status == TicketStatus.pending:
        # Pending means the team replied and is waiting on the customer. The customer has
        # answered, so it's the team's turn again.
        ticket.status = TicketStatus.open

    # Setting message.ticket, rather than appending to ticket.messages, avoids loading every
    # earlier message in the conversation just to add one.
    db.add(
        Message(
            ticket=ticket,
            sender=MessageSender.customer,
            body=message.body or NO_TEXT,
            twilio_sid=message.message_sid,
        )
    )
    return ticket, is_new


@router.post(
    "/twilio",
    response_class=Response,
    responses={200: {"content": {"application/xml": {}}, "description": "TwiML for Twilio"}},
)
def receive_whatsapp_message(
    message: Annotated[WhatsAppMessage, Form()], db: SessionDep, redis: RedisDep, bot: BotDep
):
    """Twilio calls this for each WhatsApp message a customer sends.

    The bot answers first, with the help docs it answered from, if it can. Otherwise the
    customer is found by phone number, or added. The message joins their open ticket, or
    opens a new one if they have none. A new ticket gets a confirmation reply saying someone
    will follow up, which Twilio sends to the customer on WhatsApp.

    A message Twilio delivers again isn't stored again. It gets the same reply as the first
    delivery, because Twilio only repeats a delivery when it didn't use the first reply.
    """
    reply = MessagingResponse()
    answer = ask_bot(db, redis, bot, message)
    if answer is not None:
        # Nothing is stored: a question the bot answered needs no one from the team.
        reply.message(bot_reply(answer))
        return twiml(reply)

    ticket, is_new = add_to_ticket(db, message)
    db.commit()
    clear_cache(redis, STATS_KEY)  # A ticket may have opened, or gone from pending to open.

    # Only when a ticket opens. Someone who sends five short messages in a row shouldn't get
    # five confirmations, and later messages join a ticket they already know about. An empty
    # <Response/> tells Twilio there's nothing to send.
    if is_new:
        reply.message(confirmation(ticket.id))
    return twiml(reply)
