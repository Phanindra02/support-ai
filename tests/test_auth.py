"""Agents log in with an email and password, and prove who they are with a signed JWT."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.models import Agent
from app.security import ALGORITHM, DUMMY_HASH, hash_password, verify_password


@pytest.fixture
def client(anonymous_client):
    """These tests log in, or send their own tokens, so they start logged out."""
    return anonymous_client


def log_in(client, email, password):
    # A form, not JSON: the OAuth2 standard says so, and /docs's Authorize button sends one.
    return client.post("/auth/token", data={"username": email, "password": password})


def make_token(secret=None, algorithm=ALGORITHM, **claims):
    """A token with exactly these claims, for tests that need a bad one."""
    return jwt.encode(claims, secret or settings.jwt_secret_key, algorithm=algorithm)


def in_an_hour():
    return datetime.now(timezone.utc) + timedelta(hours=1)


def me(client, token):
    return client.get("/agents/me", headers={"Authorization": f"Bearer {token}"})


def assert_401(response, detail):
    assert response.status_code == 401
    assert response.json() == {"detail": detail}
    # Tells the client how to authenticate: log in and send a Bearer token.
    assert response.headers["WWW-Authenticate"] == "Bearer"


# Password hashing


def test_a_hash_is_not_the_password_and_differs_each_time():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert "correct horse" not in first
    assert first.startswith("$argon2id$")
    # Each hash has its own random salt, so equal passwords don't give equal hashes.
    assert first != second


def test_verify_accepts_only_the_right_password(agent_password, agent_password_hash):
    assert verify_password(agent_password, agent_password_hash)
    assert not verify_password(agent_password + "!", agent_password_hash)
    assert not verify_password(agent_password.upper(), agent_password_hash)


# Logging in


def test_login_returns_a_bearer_token_for_the_agent(client, make_agent, agent_password):
    agent = make_agent()
    before = datetime.now(timezone.utc).replace(microsecond=0)

    response = log_in(client, "sam.lee@example.com", agent_password)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    claims = jwt.decode(body["access_token"], settings.jwt_secret_key, algorithms=[ALGORITHM])
    assert claims["sub"] == str(agent.id)
    expires = datetime.fromtimestamp(claims["exp"], timezone.utc)
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    assert before + lifetime <= expires <= datetime.now(timezone.utc) + lifetime


def test_the_email_ignores_capitals_and_spaces(client, make_agent, agent_password):
    make_agent()
    assert log_in(client, "  Sam.Lee@Example.COM ", agent_password).status_code == 200


def test_a_wrong_password_gets_a_401(client, make_agent, agent_password):
    make_agent()
    response = log_in(client, "sam.lee@example.com", "wrong password")
    assert_401(response, "Incorrect email or password")


def test_an_unknown_email_gets_the_same_401(client, make_agent, agent_password):
    make_agent()
    response = log_in(client, "nobody@example.com", agent_password)
    assert_401(response, "Incorrect email or password")


def test_an_unknown_email_still_takes_as_long_as_checking_a_password(client, monkeypatch):
    checked = []

    def spy(password, hashed):
        checked.append(hashed)
        return verify_password(password, hashed)

    monkeypatch.setattr("app.security.verify_password", spy)
    log_in(client, "nobody@example.com", "any password")
    # A hash was checked even though there was no agent, so a timer can't tell the
    # difference between a wrong email and a wrong password.
    assert checked == [DUMMY_HASH]


def test_an_agent_without_a_password_cannot_log_in(client, make_agent, monkeypatch):
    make_agent(password_hash=None)
    checked = []
    monkeypatch.setattr("app.security.verify_password", lambda p, h: checked.append(h))
    response = log_in(client, "sam.lee@example.com", "any password")
    assert_401(response, "Incorrect email or password")
    assert checked == [DUMMY_HASH]  # Takes as long as a real check here too.


def test_a_deactivated_agent_cannot_log_in(client, make_agent, agent_password):
    make_agent(is_active=False)
    assert_401(
        log_in(client, "sam.lee@example.com", agent_password), "Incorrect email or password"
    )


@pytest.mark.parametrize("email", ["sam\x00@example.com", "x" * 10_000, "' OR 1=1 --"])
def test_odd_emails_get_a_401_not_a_500(client, email, agent_password):
    assert_401(log_in(client, email, agent_password), "Incorrect email or password")


@pytest.mark.parametrize("missing", ["username", "password"])
def test_a_missing_field_gets_a_422(client, missing):
    form = {"username": "sam.lee@example.com", "password": "secret"}
    del form[missing]
    response = client.post("/auth/token", data=form)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", missing]


def test_json_instead_of_a_form_gets_a_422(client, make_agent, agent_password):
    make_agent()
    response = client.post(
        "/auth/token", json={"username": "sam.lee@example.com", "password": agent_password}
    )
    assert response.status_code == 422


# Using the token


def test_a_token_from_login_identifies_the_agent(client, make_agent, agent_password):
    agent = make_agent()
    token = log_in(client, "sam.lee@example.com", agent_password).json()["access_token"]

    response = me(client, token)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": agent.id,
        "name": "Sam Lee",
        "email": "sam.lee@example.com",
        "role": "agent",
        "is_active": True,
        "created_at": body["created_at"],
    }
    assert "password_hash" not in body


def test_no_token_gets_a_401(client):
    assert_401(client.get("/agents/me"), "Not authenticated")


def test_a_header_that_is_not_bearer_gets_a_401(client):
    response = client.get("/agents/me", headers={"Authorization": "Basic c2FtOnNlY3JldA=="})
    assert_401(response, "Not authenticated")


@pytest.mark.parametrize("token", ["not-a-token", "a.b.c", "e30.e30.e30", "%%%.%%%.%%%"])
def test_a_garbled_token_gets_a_401(client, token):
    assert_401(me(client, token), "Could not validate credentials")


def test_an_expired_token_gets_a_401(client, make_agent):
    agent = make_agent()
    an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    token = make_token(sub=str(agent.id), exp=an_hour_ago)
    assert_401(me(client, token), "Could not validate credentials")


def test_a_token_signed_with_another_key_gets_a_401(client, make_agent):
    agent = make_agent()
    other_key = "a-different-key-that-is-also-32-bytes-or-more"
    token = make_token(secret=other_key, sub=str(agent.id), exp=in_an_hour())
    assert_401(me(client, token), "Could not validate credentials")


def test_an_unsigned_token_gets_a_401(client, make_agent):
    # "alg": "none" means no signature. Accepting it would let anyone write their own token.
    agent = make_agent()
    token = jwt.encode({"sub": str(agent.id), "exp": in_an_hour()}, None, algorithm="none")
    assert_401(me(client, token), "Could not validate credentials")


def test_a_token_whose_claims_were_edited_gets_a_401(client, make_agent):
    sam = make_agent()
    boss = make_agent(name="Boss", email="boss@example.com")
    header, _, signature = make_token(sub=str(sam.id), exp=in_an_hour()).split(".")
    # Swap in a payload naming another agent, keeping Sam's signature.
    _, payload, _ = make_token(sub=str(boss.id), exp=in_an_hour()).split(".")
    assert_401(me(client, f"{header}.{payload}.{signature}"), "Could not validate credentials")


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "1"},  # No expiry: it would work forever.
        {"exp": "in an hour"},
        {"sub": "sam", "exp": "in an hour"},
        {"sub": "0", "exp": "in an hour"},
        {"sub": "99999999999", "exp": "in an hour"},
        {"sub": "-1", "exp": "in an hour"},
    ],
)
def test_a_signed_token_with_bad_claims_gets_a_401(client, make_agent, claims):
    make_agent()
    claims = {k: in_an_hour() if v == "in an hour" else v for k, v in claims.items()}
    assert_401(me(client, make_token(**claims)), "Could not validate credentials")


def test_a_deactivated_agent_is_locked_out_at_once(
    client, make_agent, session_factory, auth_header
):
    agent = make_agent()
    headers = auth_header(agent)
    assert client.get("/agents/me", headers=headers).status_code == 200

    with session_factory() as db:
        db.get(Agent, agent.id).is_active = False
        db.commit()

    # The token hasn't expired, but the agent is checked on every request.
    assert_401(client.get("/agents/me", headers=headers), "Could not validate credentials")


def test_a_token_for_a_deleted_agent_gets_a_401(client, make_agent, session_factory, auth_header):
    agent = make_agent()
    headers = auth_header(agent)
    with session_factory() as db:
        db.delete(db.get(Agent, agent.id))
        db.commit()
    assert_401(client.get("/agents/me", headers=headers), "Could not validate credentials")


def test_docs_know_where_to_log_in(client):
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "auth/token"
