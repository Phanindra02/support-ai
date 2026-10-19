from fastapi import APIRouter

from app.schemas import AgentRead, ErrorResponse
from app.security import CurrentAgent

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    responses={401: {"model": ErrorResponse, "description": "Not logged in"}},
)


@router.get("/me", response_model=AgentRead)
def read_me(agent: CurrentAgent):
    """The agent who is logged in."""
    return agent
