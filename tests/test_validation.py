"""The input rules: text lengths, phone format, extra fields, number limits, and safe 422s."""

import json

import pytest

from app.schemas import MAX_INT
from app.models import Customer, Message, MessageSender, Ticket

TEXT_LIMITS = [("customer_name", 100), ("subject", 200), ("message", 5000)]
TEXT_FIELDS = [field for field, _ in TEXT_LIMITS]


def error_types(response):
    """The Pydantic error types in a 422 response, like ["string_too_short"]."""
    return [error["type"] for error in response.json()["detail"]]


def saved_value(client, ticket, field):
    """Where a TicketCreate field is stored: on the customer, the ticket, or the first message."""
    if field == "customer_name":
        return ticket["customer"]["name"]
    if field == "phone":
        return ticket["customer"]["phone"]
    if field == "message":
        return client.get(f"/tickets/{ticket['id']}/messages").json()[0]["body"]
    return ticket[field]


def strict_json(text):
    """Parse JSON the way a strict client (like a browser) would, so NaN and Infinity fail."""

    def reject(constant):
        raise ValueError(f"{constant} is not valid JSON")

    return json.loads(text, parse_constant=reject)


# Text fields


@pytest.mark.parametrize("field, limit", TEXT_LIMITS)
def test_text_at_the_length_limits_is_accepted(client, ticket_data, field, limit):
    # A new phone number each time, because a returning customer keeps their first name.
    for phone, value in [("+16145550101", "a"), ("+16145550102", "a" * limit)]:
        response = client.post("/tickets", json={**ticket_data, "phone": phone, field: value})
        assert response.status_code == 201, response.text
        assert saved_value(client, response.json(), field) == value


@pytest.mark.parametrize("field, limit", TEXT_LIMITS)
def test_text_over_the_length_limit_is_rejected(client, ticket_data, field, limit):
    response = client.post("/tickets", json={**ticket_data, field: "a" * (limit + 1)})
    assert response.status_code == 422
    assert error_types(response) == ["string_too_long"]
    assert response.json()["detail"][0]["loc"] == ["body", field]


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_empty_or_blank_text_is_rejected(client, ticket_data, field, value):
    response = client.post("/tickets", json={**ticket_data, field: value})
    assert response.status_code == 422
    assert error_types(response) == ["string_too_short"]


def test_spaces_are_trimmed_before_saving(client, ticket_data):
    response = client.post("/tickets", json={**ticket_data, "customer_name": "  Jane Doe  "})
    assert response.status_code == 201
    assert response.json()["customer"]["name"] == "Jane Doe"


@pytest.mark.parametrize("field, limit", TEXT_LIMITS)
def test_length_is_checked_after_trimming(client, ticket_data, field, limit):
    padded = "  " + "a" * limit + "  "
    response = client.post("/tickets", json={**ticket_data, field: padded})
    assert response.status_code == 201
    assert saved_value(client, response.json(), field) == "a" * limit


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_nul_character_is_rejected(client, ticket_data, field):
    response = client.post("/tickets", json={**ticket_data, field: "a\x00b"})
    assert response.status_code == 422
    assert "NUL" in response.json()["detail"][0]["msg"]


def test_text_must_be_a_string(client, ticket_data):
    response = client.post("/tickets", json={**ticket_data, "customer_name": 123})
    assert response.status_code == 422
    assert error_types(response) == ["string_type"]


# Phone numbers


@pytest.mark.parametrize(
    "phone",
    [
        "+16145550123",
        "+447911123456",
        "+12",  # The shortest E.164 number: 2 digits.
        "+123456789012345",  # The longest: 15 digits.
        "  +16145550123  ",  # Spaces around it are trimmed.
    ],
)
def test_valid_phone_numbers_are_accepted(client, ticket_data, phone):
    response = client.post("/tickets", json={**ticket_data, "phone": phone})
    assert response.status_code == 201, response.text
    assert response.json()["customer"]["phone"] == phone.strip()


@pytest.mark.parametrize(
    "phone",
    [
        "16145550123",  # No plus sign.
        "+06145550123",  # Starts with 0.
        "+1",  # Only 1 digit.
        "+1234567890123456",  # 16 digits.
        "+1 614 555 0123",  # Spaces inside.
        "+1-614-555-0123",  # Dashes.
        "+1614555012a",  # A letter.
        "+١٦١٤٥٥٥",  # Arabic-Indic digits, which \d allows.
        "",
    ],
)
def test_invalid_phone_numbers_are_rejected(client, ticket_data, phone):
    response = client.post("/tickets", json={**ticket_data, "phone": phone})
    assert response.status_code == 422
    assert error_types(response) == ["string_pattern_mismatch"]


# Required and unknown fields


@pytest.mark.parametrize("field", ["customer_name", "phone", "subject", "message"])
def test_every_create_field_is_required(client, ticket_data, field):
    del ticket_data[field]
    response = client.post("/tickets", json=ticket_data)
    assert response.status_code == 422
    assert error_types(response) == ["missing"]


@pytest.mark.parametrize("extra", [{"status": "closed"}, {"priority": "urgent"}, {"id": 5}])
def test_create_rejects_fields_a_customer_cannot_set(client, ticket_data, extra):
    response = client.post("/tickets", json={**ticket_data, **extra})
    assert response.status_code == 422
    assert error_types(response) == ["extra_forbidden"]


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '"a string"'])
def test_create_rejects_a_body_that_is_not_a_json_object(client, body):
    response = client.post(
        "/tickets", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


# Updates


@pytest.mark.parametrize("body", [{}, {"status": None}, {"status": None, "priority": None}])
def test_update_must_send_status_or_priority(client, make_ticket, body):
    ticket = make_ticket()
    response = client.patch(f"/tickets/{ticket['id']}", json=body)
    assert response.status_code == 422
    assert "Send status, priority, or both" in response.json()["detail"][0]["msg"]


def test_update_rejects_other_fields_and_changes_nothing(client, make_ticket):
    ticket = make_ticket()
    response = client.patch(
        f"/tickets/{ticket['id']}", json={"status": "closed", "subject": "Changed"}
    )
    assert response.status_code == 422
    assert error_types(response) == ["extra_forbidden"]
    assert client.get(f"/tickets/{ticket['id']}").json() == ticket


@pytest.mark.parametrize(
    "body",
    [{"status": "done"}, {"status": "OPEN"}, {"priority": "critical"}, {"priority": 1}],
)
def test_update_rejects_unknown_status_or_priority(client, make_ticket, body):
    ticket = make_ticket()
    response = client.patch(f"/tickets/{ticket['id']}", json=body)
    assert response.status_code == 422
    assert error_types(response) == ["enum"]


def test_update_without_a_body_is_rejected(client, make_ticket):
    ticket = make_ticket()
    response = client.patch(f"/tickets/{ticket['id']}")
    assert response.status_code == 422
    assert error_types(response) == ["missing"]


# Numbers in the path and query string


BAD_IDS = ["0", "-1", str(MAX_INT + 1), "99999999999999999999", "abc", "1.5"]


@pytest.mark.parametrize("ticket_id", BAD_IDS)
def test_get_rejects_ids_outside_the_valid_range(client, ticket_id):
    assert client.get(f"/tickets/{ticket_id}").status_code == 422
    assert client.get(f"/tickets/{ticket_id}/messages").status_code == 422


@pytest.mark.parametrize("ticket_id", BAD_IDS)
def test_update_rejects_ids_outside_the_valid_range(client, ticket_id):
    response = client.patch(f"/tickets/{ticket_id}", json={"status": "closed"})
    assert response.status_code == 422


def test_largest_valid_id_gets_a_404_not_a_crash(client):
    assert client.get(f"/tickets/{MAX_INT}").status_code == 404
    assert client.get(f"/tickets/{MAX_INT}/messages").status_code == 404
    response = client.patch(f"/tickets/{MAX_INT}", json={"status": "closed"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}


@pytest.mark.parametrize("query", ["skip=0", f"skip={MAX_INT}", "limit=1", "limit=100"])
def test_list_accepts_skip_and_limit_at_their_limits(client, query):
    assert client.get(f"/tickets?{query}").status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "skip=-1",
        f"skip={MAX_INT + 1}",
        "limit=0",
        "limit=101",
        "skip=abc",
        "status=done",
        "status=OPEN",
        "priority=critical",
    ],
)
def test_list_rejects_bad_query_values(client, query):
    assert client.get(f"/tickets?{query}").status_code == 422


# Input that crashed FastAPI's standard 422 with a 500, because it can't be repeated back


VALID_FIELDS = '"phone": "+16145550123", "subject": "a", "message": "b"'


@pytest.mark.parametrize(
    "body",
    [
        '{"customer_name": NaN, ' + VALID_FIELDS + "}",
        '{"customer_name": Infinity, ' + VALID_FIELDS + "}",
        '{"customer_name": 1e999, ' + VALID_FIELDS + "}",  # Too big for a float: infinity.
        '{"customer_name": "\\ud800", ' + VALID_FIELDS + "}",  # Half of an emoji.
        '{"\\ud800": 1, "customer_name": "a", ' + VALID_FIELDS + "}",
        "[" * 2000 + "]" * 2000,
    ],
    ids=["nan", "infinity", "1e999", "half-emoji-value", "half-emoji-key", "deep-nesting"],
)
def test_unrepeatable_input_gets_a_clean_422(client, body):
    response = client.post(
        "/tickets", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    errors = strict_json(response.text)["detail"]
    assert errors and all({"type", "loc", "msg"} <= error.keys() for error in errors)


def test_an_ordinary_422_keeps_fastapis_standard_shape(client, ticket_data):
    response = client.post("/tickets", json={**ticket_data, "phone": "12345"})
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "phone"]
    assert error["input"] == "12345"


# The read model and /docs


def test_a_stored_ticket_that_breaks_the_input_rules_can_still_be_read(client, session_factory):
    # Rows can come from places other than the API, like old data or a seed script.
    with session_factory() as db:
        ticket = Ticket(customer=Customer(name="", phone="614-555-0123"), subject="")
        ticket.messages.append(Message(sender=MessageSender.customer, body=""))
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.get(f"/tickets/{ticket_id}")
    assert response.status_code == 200
    assert response.json()["customer"]["phone"] == "614-555-0123"
    assert client.get("/tickets").status_code == 200
    assert client.get(f"/tickets/{ticket_id}/messages").json()[0]["body"] == ""


def test_docs_list_the_404_shape_for_single_ticket_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, method in [
        ("/tickets/{ticket_id}", "get"),
        ("/tickets/{ticket_id}", "patch"),
        ("/tickets/{ticket_id}/messages", "get"),
    ]:
        not_found = paths[path][method]["responses"]["404"]
        schema_ref = not_found["content"]["application/json"]["schema"]["$ref"]
        assert schema_ref == "#/components/schemas/ErrorResponse"


def test_docs_examples_are_valid_requests(client, make_ticket):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    [create_example] = schemas["TicketCreate"]["examples"]
    [update_example] = schemas["TicketUpdate"]["examples"]

    assert client.post("/tickets", json=create_example).status_code == 201
    ticket = make_ticket()
    assert client.patch(f"/tickets/{ticket['id']}", json=update_example).status_code == 200
