"""The Twilio webhook: customers' WhatsApp messages arrive here.

Twilio POSTs each incoming WhatsApp message to POST /webhooks/twilio as a form. The response
is TwiML, an XML format Twilio reads, and any message in it is sent back to the customer.

This router skips the per-IP rate limit. Every customer's message arrives from Twilio's
servers, so a limit per IP would hold back all customers together. The signature check
below means only Twilio can call it.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.config import settings
from app.schemas import ErrorResponse


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


@router.post(
    "/twilio",
    response_class=Response,
    responses={200: {"content": {"application/xml": {}}, "description": "TwiML for Twilio"}},
)
def receive_whatsapp_message():
    """Twilio calls this for each WhatsApp message a customer sends."""
    # An empty <Response/> tells Twilio there's nothing to send back.
    return twiml(MessagingResponse())
