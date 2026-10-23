from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)

from app.models import AgentRole, DocumentStatus, MessageSender, TicketPriority, TicketStatus

# E.164: a plus sign, then 2 to 15 digits, with a first digit that isn't 0.
# [0-9] instead of \d, because \d also matches digits from other alphabets.
E164_PATTERN = r"^\+[1-9][0-9]{1,14}$"

# The largest number a Postgres integer column can hold. No id can be bigger, so no useful
# skip can be either, and bigger numbers would crash the database call with a 500.
MAX_INT = 2_147_483_647


def reject_nul(value: str) -> str:
    """Reject the NUL character (\\u0000). PostgreSQL can't store it, so saving it would crash."""
    if "\x00" in value:
        raise ValueError("Text can't contain the NUL character (\\u0000)")
    return value


# Goes after Field(...) in each Annotated. Listed first, it would turn the clear length errors
# ("String should have at least 1 character") into vaguer ones.
NoNul = AfterValidator(reject_nul)


class TicketCreate(BaseModel):
    """What an agent sends to open a ticket for a customer, such as after a phone call.

    The server finds the customer by phone number, or adds them if they're new, and the
    message becomes the first one in the ticket's conversation.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,  # Trim spaces before checking lengths.
        extra="forbid",  # Reject fields this form doesn't have.
        json_schema_extra={
            "examples": [
                {
                    "customer_name": "Jane Doe",
                    "phone": "+16145550123",
                    "subject": "Order late",
                    "message": "Where is my order?",
                }
            ]
        },
    )

    # The limits match the column sizes in models.py.
    customer_name: Annotated[str, Field(min_length=1, max_length=100), NoNul]
    phone: Annotated[str, Field(pattern=E164_PATTERN)]
    subject: Annotated[str, Field(min_length=1, max_length=200), NoNul]
    message: Annotated[str, Field(min_length=1, max_length=5000), NoNul]


# The read models have no input rules, so every stored row can be shown.


class CustomerRead(BaseModel):
    """A customer, as shown inside each of their tickets."""

    # Lets Pydantic read values straight off a database object (customer.id, customer.name, ...).
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str


class TicketRead(BaseModel):
    """A ticket, as the API sends it back. Its messages are at /tickets/{id}/messages."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    customer: CustomerRead
    agent_id: int | None
    subject: str
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime
    closed_at: datetime | None


class MessageRead(BaseModel):
    """One message in a ticket's conversation."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sender: MessageSender
    body: str
    created_at: datetime


class StatsRead(BaseModel):
    """Ticket counts for the support team's dashboard."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "by_status": {"open": 4701, "pending": 2546, "closed": 92753},
                    "backlog_by_priority": {
                        "low": 2768,
                        "medium": 3069,
                        "high": 1030,
                        "urgent": 380,
                    },
                }
            ]
        }
    )

    # Every status, with 0 for one that no ticket has.
    by_status: dict[TicketStatus, int]
    # Tickets not closed yet (open or pending), by priority: the work still waiting.
    backlog_by_priority: dict[TicketPriority, int]


class TicketUpdate(BaseModel):
    """What an agent sends to change a ticket: status, priority, or both."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"status": "closed"}]},
    )

    status: TicketStatus | None = None
    priority: TicketPriority | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> Self:
        """Runs after the fields are checked, and rejects an update that changes nothing."""
        if self.status is None and self.priority is None:
            raise ValueError("Send status, priority, or both")
        return self


class ErrorResponse(BaseModel):
    """The shape of an error like a 404, so /docs can show it."""

    detail: str


# Agents and logins

# NIST's password guidance (SP 800-63B) asks for at least 15 characters when a password is
# the only thing protecting an account, and no rules about digits or symbols. The upper limit
# stops anyone from making the server hash a huge "password". Spaces are kept, not trimmed,
# because they're part of the password.
Password = Annotated[str, Field(min_length=15, max_length=128)]

# A valid address, lowercased so the same login can't exist twice with different capitals.
# The email checker also keeps it to 254 characters, the column size.
Email = Annotated[EmailStr, AfterValidator(str.lower)]


# Trimmed here rather than for the whole model, which would trim the password too.
AgentName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100), NoNul
]


class AgentCreate(BaseModel):
    """A new agent who can log in. They're a plain agent unless the role says admin."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Sam Lee",
                    "email": "sam.lee@example.com",
                    "password": "correct horse battery staple",
                    "role": "agent",
                }
            ]
        },
    )

    name: AgentName
    email: Email
    password: Password
    role: AgentRole = AgentRole.agent


class AgentUpdate(BaseModel):
    """What an admin sends to change an agent. The email can't change: it's their login."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"is_active": False}, {"role": "admin"}]},
    )

    name: AgentName | None = None
    password: Password | None = None
    role: AgentRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> Self:
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("Send name, password, role, is_active, or a mix of them")
        return self


class AgentRead(BaseModel):
    """An agent, as the API shows them. Never the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: AgentRole
    is_active: bool
    created_at: datetime


class UrgentAlert(BaseModel):
    """What the API sends n8n when a ticket is marked urgent, for the Slack message.

    The customer's phone number is left out: the Slack channel doesn't need it, and fewer
    copies of personal data means fewer places it can leak from.
    """

    ticket_id: int
    subject: str
    customer_name: str
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime
    marked_urgent_by: str


# WhatsApp, through Twilio


def drop_nul(value: str) -> str:
    """Remove the NUL character, which Postgres can't store.

    Unlike in the API's own forms, the text is cleaned rather than refused: a 422 would make
    Twilio drop the customer's message.
    """
    return value.replace("\x00", "")


class WhatsAppMessage(BaseModel):
    """The fields this app reads from Twilio's incoming-message webhook.

    Not extra="forbid": Twilio sends about 20 fields, and adds new ones from time to time.
    The signature check has already proved the request came from Twilio.
    """

    # Twilio's id for this message, the same on every delivery of it. Twilio's ids are 34
    # letters and digits. Checked loosely, since a real message turned away is lost.
    message_sid: Annotated[str, Field(alias="MessageSid", pattern=r"^[A-Za-z0-9]{1,34}$")]
    # The customer: "whatsapp:" and their number in E.164, like whatsapp:+16145550123.
    sender: Annotated[str, Field(alias="From", pattern=r"^whatsapp:\+[1-9][0-9]{1,14}$")]
    # Empty when the customer sent only a photo, file, or location.
    body: Annotated[str, Field(alias="Body"), AfterValidator(drop_nul)] = ""
    # The name on the customer's WhatsApp profile, if they've set one.
    profile_name: Annotated[str, Field(alias="ProfileName"), AfterValidator(drop_nul)] = ""

    @property
    def phone(self) -> str:
        return self.sender.removeprefix("whatsapp:")


class Token(BaseModel):
    """What a successful login returns. The field names come from the OAuth2 standard."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"access_token": "eyJhbGciOi...", "token_type": "bearer"}]}
    )

    access_token: str
    token_type: str


# Help docs


class DocumentRead(BaseModel):
    """An uploaded help doc, as the API shows it. Where it is in S3 only the API needs."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    size_bytes: int
    status: DocumentStatus
    uploaded_by_id: int
    created_at: datetime


# What the API makes up for each upload (app/storage.py), and where the Lambda puts its text:
# the same 32 hex digits in both.
UPLOAD_KEY_PATTERN = r"^uploads/[0-9a-f]{32}\.pdf$"
TEXT_KEY_PATTERN = r"^text/[0-9a-f]{32}\.txt$"


class DocumentProcessed(BaseModel):
    """What the Lambda sends once it has saved a PDF's text to S3 (lambda/extract_text.py).

    The Lambda only knows the key S3 told it about, not the document's id, so the key finds
    the row. The keys must look like the ones the API and the Lambda make.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "s3_key": "uploads/70208fbd9d4a4cbc8cbbc7cb6f3b001c.pdf",
                    "text_s3_key": "text/70208fbd9d4a4cbc8cbbc7cb6f3b001c.txt",
                }
            ]
        },
    )

    s3_key: Annotated[str, Field(pattern=UPLOAD_KEY_PATTERN)]
    text_s3_key: Annotated[str, Field(pattern=TEXT_KEY_PATTERN)]
