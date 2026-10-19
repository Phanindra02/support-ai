import json

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from app.rate_limit import rate_limit
from app.routers import agents, auth, tickets
from app.schemas import ErrorResponse

# Tables come from Alembic migrations (uv run alembic upgrade head), not from the app itself.
app = FastAPI(title="Support AI")

# Every endpoint on this router checks the rate limit first. /docs isn't an endpoint, so it
# isn't limited.
api = APIRouter(
    dependencies=[Depends(rate_limit)],
    responses={429: {"model": ErrorResponse, "description": "Too many requests from this IP"}},
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    """Return FastAPI's standard 422, even for input that can't be repeated back.

    The standard 422 repeats each bad input. Python's JSON parser accepts some input it can't
    write back out: NaN, Infinity, half of an emoji like "\\ud800", or lists nested 1,000
    deep. Repeating those crashed the error response itself with a 500.
    """
    try:
        return await request_validation_exception_handler(request, exc)
    except (ValueError, RecursionError):  # UnicodeEncodeError is a kind of ValueError.
        # Send the same errors without the input. loc can hold a JSON key the client sent, so
        # ensure_ascii=True writes any half-emoji in it as a plain "\ud800" escape.
        errors = [{"type": e["type"], "loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
        body = json.dumps({"detail": errors}, ensure_ascii=True)
        return Response(body, status_code=422, media_type="application/json")


@api.get("/")
def root():
    return {"message": "Support API is running"}


api.include_router(auth.router)
api.include_router(agents.router)
api.include_router(tickets.router)
app.include_router(api)
