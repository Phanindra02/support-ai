from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from app.database import SessionDep
from app.schemas import ErrorResponse, Token
from app.security import authenticate_agent, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/token",
    response_model=Token,
    responses={401: {"model": ErrorResponse, "description": "Wrong email or password"}},
)
def log_in(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: SessionDep):
    """Log in with an agent's email and password, and get an access token.

    The OAuth2 standard names the form field `username`, so the email goes there. Send the
    token on later requests in the header `Authorization: Bearer <token>`. In /docs, the
    Authorize button does this for you.
    """
    agent = authenticate_agent(db, form.username.strip(), form.password)
    if agent is None:
        # The same message whether the email or the password is wrong, so a failed login
        # doesn't reveal which emails have accounts.
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Token(access_token=create_access_token(agent), token_type="bearer")
