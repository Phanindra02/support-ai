"""Add an agent who can log in, or give an existing agent a new password.

Run it from the project root. It asks for the password rather than taking it as an
argument, so the password never ends up in your shell history:

    uv run python -m scripts.create_agent --email sam.lee@example.com --name "Sam Lee"

The seed script's agents have no password. To log in as one, run this with their email.
"""

import argparse
import sys
from getpass import getpass

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Agent
from app.schemas import AgentCreate
from app.security import find_agent_by_email, hash_password


def save_agent(db: Session, agent_in: AgentCreate) -> tuple[Agent, bool]:
    """Add the agent, or update the one with this email. Also says whether they're new."""
    agent = find_agent_by_email(db, agent_in.email)
    is_new = agent is None
    if agent is None:
        agent = Agent(email=agent_in.email)
        db.add(agent)
    agent.name = agent_in.name
    agent.password_hash = hash_password(agent_in.password)
    agent.is_active = True
    db.commit()
    return agent, is_new


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Add an agent who can log in, or give an agent a new password."
    )
    parser.add_argument("--email", required=True, help="what the agent logs in with")
    parser.add_argument("--name", help="the agent's full name (needed for a new agent)")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        existing = find_agent_by_email(db, args.email)
        name = args.name or (existing.name if existing else None)
        if name is None:
            parser.error("--name is needed for a new agent")

        password = getpass("Password (at least 15 characters): ")
        if getpass("Type it again: ") != password:
            sys.exit("The passwords don't match. Nothing was changed.")
        try:
            agent_in = AgentCreate(name=name, email=args.email, password=password)
        except ValidationError as error:
            problems = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()]
            sys.exit("Nothing was changed.\n" + "\n".join(problems))

        agent, is_new = save_agent(db, agent_in)
        print(f"{'Added' if is_new else 'Updated'} {agent.email}. They can log in now.")


if __name__ == "__main__":
    main()
