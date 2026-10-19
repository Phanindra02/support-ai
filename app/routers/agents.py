from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionDep
from app.models import Agent, AgentRole
from app.schemas import MAX_INT, AgentCreate, AgentRead, AgentUpdate, ErrorResponse
from app.security import CurrentAdmin, CurrentAgent, hash_password

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    responses={401: {"model": ErrorResponse, "description": "Not logged in"}},
)

AgentId = Annotated[int, Path(ge=1, le=MAX_INT)]

# For /docs: the errors only the admin endpoints can return.
ADMIN_ONLY: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Not an admin"}
}


@router.get("/me", response_model=AgentRead)
def read_me(agent: CurrentAgent):
    """The agent who is logged in."""
    return agent


@router.get("", response_model=list[AgentRead], responses=ADMIN_ONLY)
def list_agents(db: SessionDep, admin: CurrentAdmin):
    """Every agent, including deactivated ones, in the order they were added."""
    # No pagination: a support team is tens of people, not thousands.
    return db.scalars(select(Agent).order_by(Agent.id)).all()


@router.post(
    "",
    response_model=AgentRead,
    status_code=201,
    responses={
        **ADMIN_ONLY,
        409: {"model": ErrorResponse, "description": "The email is taken"},
    },
)
def create_agent(agent_in: AgentCreate, db: SessionDep, admin: CurrentAdmin):
    """Add an agent who can log in straight away. Only admins can do this."""
    agent = Agent(
        name=agent_in.name,
        email=agent_in.email,
        password_hash=hash_password(agent_in.password),
        role=agent_in.role,
    )
    db.add(agent)
    # Let the unique constraint on email catch a taken address. Checking first and then
    # inserting would leave a gap where two requests could both pass the check.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An agent with this email already exists")
    db.refresh(agent)
    return agent


@router.patch(
    "/{agent_id}",
    response_model=AgentRead,
    responses={
        **ADMIN_ONLY,
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
)
def update_agent(agent_id: AgentId, agent_in: AgentUpdate, db: SessionDep, admin: CurrentAdmin):
    """Change an agent's name, password, role, or whether they can log in.

    Deactivating an agent locks them out on their next request, even with a token that
    hasn't expired. Admins can't demote or deactivate themselves, so there's always at
    least one admin left.
    """
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.id == admin.id and (
        agent_in.role == AgentRole.agent or agent_in.is_active is False
    ):
        raise HTTPException(
            status_code=403,
            detail="Admins can't demote or deactivate themselves. Ask another admin.",
        )

    changes = agent_in.model_dump(exclude_none=True)
    if "password" in changes:
        agent.password_hash = hash_password(changes.pop("password"))
    for field, value in changes.items():
        setattr(agent, field, value)
    db.commit()
    db.refresh(agent)
    return agent
