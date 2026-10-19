"""scripts/create_agent.py adds an agent who can log in, or sets a new password."""

import pytest
from sqlalchemy import func, select

from app.models import Agent
from app.security import verify_password
from scripts import create_agent

PASSWORD = "a long enough passphrase"


@pytest.fixture
def run(session_factory, monkeypatch):
    """Run the script on the test database, typing these answers at its password prompts."""

    def run_script(*args, answers=(PASSWORD, PASSWORD)):
        typed = iter(answers)
        monkeypatch.setattr(create_agent, "SessionLocal", session_factory)
        monkeypatch.setattr(create_agent, "getpass", lambda prompt: next(typed))
        create_agent.main(list(args))

    return run_script


def the_agent(session_factory):
    with session_factory() as db:
        return db.scalars(select(Agent)).one()


def count_agents(session_factory):
    with session_factory() as db:
        return db.scalar(select(func.count()).select_from(Agent))


def test_adds_an_agent_who_can_log_in(run, client, session_factory, capsys):
    run("--email", "Sam.Lee@Example.com", "--name", "  Sam Lee ")

    agent = the_agent(session_factory)
    assert (agent.name, agent.email, agent.is_active) == ("Sam Lee", "sam.lee@example.com", True)
    assert verify_password(PASSWORD, agent.password_hash)
    assert "Added sam.lee@example.com" in capsys.readouterr().out

    form = {"username": "sam.lee@example.com", "password": PASSWORD}
    assert client.post("/auth/token", data=form).status_code == 200


def test_sets_a_password_for_a_seeded_agent_and_keeps_their_name(
    run, make_agent, session_factory, capsys
):
    make_agent(password_hash=None, is_active=False)

    run("--email", "sam.lee@example.com")

    agent = the_agent(session_factory)
    assert agent.name == "Sam Lee"
    assert agent.is_active
    assert verify_password(PASSWORD, agent.password_hash)
    assert "Updated sam.lee@example.com" in capsys.readouterr().out


def test_mismatched_passwords_change_nothing(run, session_factory):
    with pytest.raises(SystemExit, match="don't match"):
        run("--email", "sam@example.com", "--name", "Sam", answers=(PASSWORD, PASSWORD + "x"))
    assert count_agents(session_factory) == 0


def test_a_short_password_changes_nothing(run, session_factory):
    with pytest.raises(SystemExit, match="at least 15 characters"):
        run("--email", "sam@example.com", "--name", "Sam", answers=("too short", "too short"))
    assert count_agents(session_factory) == 0


def test_a_bad_email_changes_nothing(run, session_factory):
    with pytest.raises(SystemExit, match="email"):
        run("--email", "not-an-email", "--name", "Sam")
    assert count_agents(session_factory) == 0


def test_a_new_agent_needs_a_name(run, session_factory, capsys):
    with pytest.raises(SystemExit):
        run("--email", "sam@example.com")
    assert "--name is needed" in capsys.readouterr().err
    assert count_agents(session_factory) == 0
