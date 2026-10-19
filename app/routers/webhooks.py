"""The Twilio webhook: customers' WhatsApp messages arrive here.

Twilio POSTs each incoming WhatsApp message to POST /webhooks/twilio as a form. The response
is TwiML, an XML format Twilio reads, and any message in it is sent back to the customer.

This router skips the per-IP rate limit. Every customer's message arrives from Twilio's
servers, so a limit per IP would hold back all customers together. The signature check
below means only Twilio can call it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.cache import RedisDep, clear_cache
from app.config import settings
from app.database import SessionDep
from app.models import Customer, Message, MessageSender, Ticket, TicketStatus
from app.routers.tickets import STATS_KEY, get_or_create_customer
from app.schemas import ErrorResponse, WhatsAppMessage

# A new ticket's subject is the first line of the customer's message, cut to this length.
SUBJECT_LENGTH = 80
# Stored as the message when the customer sent no text, so the conversation still shows it.
NO_TEXT = "(No text. The customer sent something else, such as a photo.)"


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
    # The URL must be the public one Twilio called. Behind ngrok (or later, Caddy), the host
    # comes from the Host header the proxy passes on, and uvicorn takes the https scheme from
    # the proxy's X-Forwarded-Proto header, which it trusts from 127.0.0.1.
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
            Ticket.status.in_([TicketStatus.open, TicketStatus.pending]),
        )
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(1)
    )


def add_to_ticket(db: Session, message: WhatsAppMessage) -> Ticket:
    """Add a customer's message to their open ticket, or open a new ticket with it."""
    # A customer without a WhatsApp profile name goes by their number until an agent knows more.
    name = message.profile_name.strip()[:100] or message.phone
    customer = get_or_create_customer(db, name, message.phone)

    # Lock the customer's row until this request commits. WhatsApp users often send a few short
    # messages in a row, and Twilio delivers them at the same moment. Without the lock, two
    # requests could both find no open ticket, and open two. With it, the second waits, then
    # finds the ticket the first one opened.
    db.refresh(customer, with_for_update=True)

    ticket = find_open_ticket(db, customer)
    if ticket is None:
        ticket = Ticket(customer=customer, subject=subject_from(message.body))
        db.add(ticket)
    elif ticket.status == TicketStatus.pending:
        # Pending means the team replied and is waiting on the customer. The customer has
        # answered, so it's the team's turn again.
        ticket.status = TicketStatus.open

    # Setting message.ticket, rather than appending to ticket.messages, avoids loading every
    # earlier message in the conversation just to add one.
    db.add(Message(ticket=ticket, sender=MessageSender.customer, body=message.body or NO_TEXT))
    return ticket


@router.post(
    "/twilio",
    response_class=Response,
    responses={200: {"content": {"application/xml": {}}, "description": "TwiML for Twilio"}},
)
def receive_whatsapp_message(
    message: Annotated[WhatsAppMessage, Form()], db: SessionDep, redis: RedisDep
):
    """Twilio calls this for each WhatsApp message a customer sends.

    The customer is found by phone number, or added. The message joins their open ticket, or
    opens a new one if they have none.
    """
    add_to_ticket(db, message)
    db.commit()
    clear_cache(redis, STATS_KEY)  # A ticket may have opened, or gone from pending to open.
    # An empty <Response/> tells Twilio there's nothing to send back.
    return twiml(MessagingResponse())
