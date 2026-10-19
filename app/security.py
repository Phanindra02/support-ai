"""Agent logins: password hashing, JWT access tokens, and the dependency that checks them.

Follows FastAPI's security tutorial, "OAuth2 with Password (and hashing), Bearer with JWT
tokens". An agent logs in at POST /auth/token with their email and password, and gets a
signed token. Each later request sends it in the header "Authorization: Bearer <token>".
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionDep
from app.models import Agent
from app.schemas import MAX_INT

# HMAC with SHA-256: the same secret key signs and checks tokens. Only this API makes and
# reads them, so it doesn't need RS256's separate public key.
ALGORITHM = "HS256"

# Argon2id, the algorithm pwdlib recommends. Each hash gets its own random salt, and is slow
# and memory-hungry on purpose (about 30 ms and 64 MB here), so someone who steals the table
# can only try a few guesses per second for each password.
password_hash = PasswordHash.recommended()

# Checked when the email matches no one, so a wrong email takes as long as a wrong password.
# Otherwise response times would reveal which emails have accounts.
DUMMY_HASH = password_hash.hash("dummy password")

# Reads the token from the Authorization header, and tells /docs where to log in.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def find_agent_by_email(db: Session, email: str) -> Agent | None:
    return db.scalar(select(Agent).where(Agent.email == email.lower()))


def authenticate_agent(db: Session, email: str, password: str) -> Agent | None:
    """The agent with this email and password, or None if they're wrong or can't log in."""
    # Postgres can't compare text holding the NUL character, so no email with one exists.
    agent = None if "\x00" in email else find_agent_by_email(db, email)
    if agent is None or agent.password_hash is None or not agent.is_active:
        verify_password(password, DUMMY_HASH)
        return None
    if not verify_password(password, agent.password_hash):
        return None
    return agent


def create_access_token(agent: Agent) -> str:
    """A signed token that proves who the agent is until it expires."""
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    # sub (subject) says who the token is about. JWT requires a string. The id is used
    # rather than the email, because an id never changes.
    claims = {"sub": str(agent.id), "exp": expires}
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=ALGORITHM)


def credentials_error() -> HTTPException:
    # The WWW-Authenticate header tells the client to log in and send a Bearer token.
    return HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_agent(token: Annotated[str, Depends(oauth2_scheme)], db: SessionDep) -> Agent:
    """The agent who sent this request. A missing, bad, or expired token gets a 401."""
    try:
        # algorithms is a list of the only ones accepted. Without it, a forged token could
        # pick its own, such as "none", which means no signature at all.
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
        agent_id = int(claims["sub"])
    except (InvalidTokenError, ValueError):
        raise credentials_error()

    # Look the agent up on every request, rather than trusting the token alone. A token
    # can't be taken back once it's issued, but this way an agent who is deactivated, or
    # deleted, is locked out at once instead of when their token expires.
    agent = db.get(Agent, agent_id) if 1 <= agent_id <= MAX_INT else None
    if agent is None or not agent.is_active:
        raise credentials_error()
    return agent


CurrentAgent = Annotated[Agent, Depends(get_current_agent)]
