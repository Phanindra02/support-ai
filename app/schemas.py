from datetime import datetime
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.models import TicketPriority, TicketStatus

# E.164: a plus sign, then 2 to 15 digits, with a first digit that isn't 0.
# [0-9] instead of \d, because \d also matches digits from other alphabets.
E164_PATTERN = r"^\+[1-9][0-9]{1,14}$"


def reject_nul(value: str) -> str:
    """Reject the NUL character (\\u0000). PostgreSQL can't store it, so saving it would crash."""
    if "\x00" in value:
        raise ValueError("Text can't contain the NUL character (\\u0000)")
    return value


# Goes after Field(...) in each Annotated. Listed first, it would turn the clear length errors
# ("String should have at least 1 character") into vaguer ones.
NoNul = AfterValidator(reject_nul)


class TicketCreate(BaseModel):
    """What a customer sends to open a ticket."""

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


class TicketRead(BaseModel):
    """What the API sends back. It has no input rules, so every stored ticket can be shown."""

    # Lets Pydantic read values straight off a database object (ticket.id, ticket.status, ...).
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_name: str
    phone: str
    subject: str
    message: str
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime
    closed_at: datetime | None


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
