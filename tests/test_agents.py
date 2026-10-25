"""Login is required for agent endpoints. Agents work on tickets, and admins manage agents."""

import pytest
from fastapi.testclient import TestClient

from app.models import AgentRole, Ticket
from app.routers.tickets import get_or_create_customer

# The only endpoints that don't need an agent's login. Twilio's webhook proves who sent it with
# a signature instead.
# No agent login. The webhooks check something else instead: Twilio's signature, or the
# Lambda's shared secret (tests/test_whatsapp.py and tests/test_document_processed.py).
PUBLIC = {
    ("GET", "/"),
    ("GET", "/health"),
    ("POST", "/auth/token"),
    ("POST", "/webhooks/twilio"),
    ("POST", "/webhooks/document-processed"),
}

TICKET_ENDPOINTS = [
    ("POST", "/tickets"),
    ("GET", "/tickets"),
    ("GET", "/tickets/1"),
    ("GET", "/tickets/1/messages"),
    ("PATCH", "/tickets/1"),
    ("GET", "/stats"),
]
ADMIN_ENDPOINTS = [("GET", "/agents"), ("POST", "/agents"), ("PATCH", "/agents/1")]

NEW_AGENT = {
    "name": "Sam Lee",
    "email": "sam.lee@example.com",
    "password": "correct horse battery staple",
}


def add_ticket(session_factory):
    with session_factory() as db:
        customer = get_or_create_customer(db, "Jane Doe", "+16145550123")
        ticket = Ticket(customer=customer, subject="Order late")
        db.add(ticket)
        db.commit()
        return ticket.id


def log_in(client, email, password):
    response = client.post("/auth/token", data={"username": email, "password": password})
    return response


def logged_in_as(test_app, email, password):
    """A test client that logged in through /auth/token, like a real one."""
    client = TestClient(test_app)
    token = log_in(client, email, password).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# Who may call what


def test_every_endpoint_needs_a_login_except_the_public_ones(client):
    # Checks the routes the app actually has, so a new endpoint that forgets the login check
    # fails here. OpenAPI lists a "security" requirement for each endpoint that checks it.
    paths = client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        for method, operation in methods.items():
            endpoint = (method.upper(), path)
            if endpoint in PUBLIC:
                assert "security" not in operation, endpoint
            else:
                assert operation.get("security") == [{"OAuth2PasswordBearer": []}], endpoint


@pytest.mark.parametrize(("method", "path"), TICKET_ENDPOINTS + ADMIN_ENDPOINTS)
def test_agent_endpoints_without_a_login_get_a_401(anonymous_client, method, path):
    response = anonymous_client.request(method, path, json={})
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


@pytest.mark.parametrize(("method", "path"), ADMIN_ENDPOINTS)
def test_agents_who_are_not_admins_get_a_403(client, method, path):
    response = client.request(method, path, json=NEW_AGENT)
    assert response.status_code == 403
    assert response.json() == {"detail": "Only admins can do this"}


def test_an_agent_can_work_on_tickets(client, session_factory):
    ticket_id = add_ticket(session_factory)
    assert client.get("/tickets").status_code == 200
    response = client.patch(f"/tickets/{ticket_id}", json={"status": "pending"})
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_an_admin_can_work_on_tickets_too(admin_client, session_factory):
    ticket_id = add_ticket(session_factory)
    response = admin_client.patch(f"/tickets/{ticket_id}", json={"priority": "high"})
    assert response.status_code == 200


def test_the_login_check_runs_before_the_input_check(anonymous_client):
    # A 422 would tell someone who isn't logged in what the endpoint expects.
    assert anonymous_client.patch("/tickets/0", json={"bad": 1}).status_code == 401


# Listing agents


def test_an_admin_lists_every_agent_in_the_order_they_were_added(admin_client, make_agent):
    make_agent(is_active=False)

    response = admin_client.get("/agents")

    assert response.status_code == 200
    agents = response.json()
    assert [(a["email"], a["role"], a["is_active"]) for a in agents] == [
        ("grace@example.com", "admin", True),
        ("sam.lee@example.com", "agent", False),
    ]
    assert all("password_hash" not in agent for agent in agents)


# Adding agents


def test_an_admin_adds_an_agent_who_can_log_in(admin_client, test_app):
    response = admin_client.post("/agents", json=NEW_AGENT)

    assert response.status_code == 201
    agent = response.json()
    assert agent == {
        "id": agent["id"],
        "name": "Sam Lee",
        "email": "sam.lee@example.com",
        "role": "agent",
        "is_active": True,
        "created_at": agent["created_at"],
    }
    sam = logged_in_as(test_app, NEW_AGENT["email"], NEW_AGENT["password"])
    assert sam.get("/agents/me").json()["id"] == agent["id"]


def test_an_admin_can_add_another_admin(admin_client):
    response = admin_client.post("/agents", json={**NEW_AGENT, "role": "admin"})
    assert response.json()["role"] == "admin"


def test_a_taken_email_gets_a_409_whatever_its_capitals(admin_client):
    assert admin_client.post("/agents", json=NEW_AGENT).status_code == 201
    response = admin_client.post("/agents", json={**NEW_AGENT, "email": "Sam.Lee@EXAMPLE.com"})
    assert response.status_code == 409
    assert response.json() == {"detail": "An agent with this email already exists"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("password", "x" * 14),
        ("password", "x" * 129),
        ("email", "not-an-email"),
        ("email", "sam@localhost"),
        ("name", "   "),
        ("name", "x" * 101),
        ("name", "Sam\x00"),
        ("role", "owner"),
        ("is_active", True),  # Not a field here: new agents are always active.
    ],
)
def test_bad_agent_input_gets_a_422(admin_client, field, value):
    assert admin_client.post("/agents", json={**NEW_AGENT, field: value}).status_code == 422


@pytest.mark.parametrize("password", ["x" * 15, "x" * 128, "  spaces count  "])
def test_passwords_of_15_to_128_characters_are_accepted_as_they_are(
    admin_client, test_app, password
):
    assert admin_client.post("/agents", json={**NEW_AGENT, "password": password}).status_code == 201
    # The password isn't trimmed, so it works exactly as it was typed.
    assert log_in(TestClient(test_app), NEW_AGENT["email"], password).status_code == 200


# Changing agents


def test_deactivating_an_agent_locks_them_out_at_once(admin_client, make_agent, auth_header):
    sam = make_agent()
    sam_headers = auth_header(sam)
    response = admin_client.patch(f"/agents/{sam.id}", json={"is_active": False})

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert admin_client.get("/tickets", headers=sam_headers).status_code == 401


def test_reactivating_an_agent_lets_them_back_in(admin_client, make_agent, auth_header):
    sam = make_agent(is_active=False)
    admin_client.patch(f"/agents/{sam.id}", json={"is_active": True})
    assert admin_client.get("/tickets", headers=auth_header(sam)).status_code == 200


def test_promoting_an_agent_lets_them_manage_agents(admin_client, make_agent, auth_header):
    sam = make_agent()
    assert admin_client.get("/agents", headers=auth_header(sam)).status_code == 403

    response = admin_client.patch(f"/agents/{sam.id}", json={"role": "admin"})

    assert response.json()["role"] == "admin"
    # The same token works, because the role is looked up on each request, not stored in it.
    assert admin_client.get("/agents", headers=auth_header(sam)).status_code == 200


def test_a_new_password_replaces_the_old_one(admin_client, make_agent, test_app, agent_password):
    sam = make_agent()
    new_password = "a brand new passphrase"
    response = admin_client.patch(f"/agents/{sam.id}", json={"password": new_password})

    assert response.status_code == 200
    anyone = TestClient(test_app)
    assert log_in(anyone, "sam.lee@example.com", agent_password).status_code == 401
    assert log_in(anyone, "sam.lee@example.com", new_password).status_code == 200


def test_an_admin_can_rename_an_agent(admin_client, make_agent):
    sam = make_agent()
    response = admin_client.patch(f"/agents/{sam.id}", json={"name": " Samuel Lee "})
    assert response.json()["name"] == "Samuel Lee"


@pytest.mark.parametrize("change", [{"role": "agent"}, {"is_active": False}])
def test_admins_cannot_demote_or_deactivate_themselves(admin_client, change):
    me = admin_client.get("/agents/me").json()

    response = admin_client.patch(f"/agents/{me['id']}", json=change)

    assert response.status_code == 403
    assert "Ask another admin" in response.json()["detail"]
    assert admin_client.get("/agents/me").json() == me


def test_admins_can_change_their_own_name_and_password(admin_client):
    me = admin_client.get("/agents/me").json()
    change = {"name": "Grace B. Hopper", "password": "another long passphrase", "role": "admin"}
    response = admin_client.patch(f"/agents/{me['id']}", json=change)
    assert response.status_code == 200
    assert response.json()["name"] == "Grace B. Hopper"


def test_another_admin_can_demote_an_admin(admin_client, make_agent):
    other = make_agent(role=AgentRole.admin)
    response = admin_client.patch(f"/agents/{other.id}", json={"role": "agent"})
    assert response.json()["role"] == "agent"


def test_changing_a_missing_agent_gets_a_404(admin_client):
    response = admin_client.patch("/agents/999", json={"is_active": False})
    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"is_active": None},
        {"email": "new@example.com"},  # The email is the login, so it can't change.
        {"password": "short"},
        {"role": "owner"},
    ],
)
def test_bad_changes_get_a_422(admin_client, make_agent, body):
    sam = make_agent()
    assert admin_client.patch(f"/agents/{sam.id}", json=body).status_code == 422


@pytest.mark.parametrize("agent_id", ["0", "-1", "2147483648", "abc"])
def test_agent_ids_out_of_range_get_a_422(admin_client, agent_id):
    response = admin_client.patch(f"/agents/{agent_id}", json={"is_active": False})
    assert response.status_code == 422
