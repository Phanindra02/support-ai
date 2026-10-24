"""POST /ask: put a question to the bot, which answers from the help docs (app/bot.py).

Customers reach the bot through WhatsApp. This endpoint lets agents try it out, and is how the
bot's accuracy is measured.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from openai import OpenAIError

from app.bot import BotDep, answer_with_cache
from app.cache import RedisDep
from app.database import SessionDep
from app.schemas import AskRequest, AskResponse, ErrorResponse
from app.security import get_current_agent

logger = logging.getLogger(__name__)

# Every question costs an OpenAI call, so only logged-in agents may ask here. Otherwise anyone
# who found the URL could run up the bill, 60 questions a minute from each IP address.
router = APIRouter(
    prefix="/ask",
    tags=["bot"],
    dependencies=[Depends(get_current_agent)],
    responses={401: {"model": ErrorResponse, "description": "Not logged in"}},
)


@router.post(
    "",
    response_model=AskResponse,
    responses={
        503: {"model": ErrorResponse, "description": "OpenAI isn't set up, or can't be reached"}
    },
)
def ask(body: AskRequest, db: SessionDep, redis: RedisDep, bot: BotDep, response: Response):
    """Ask the bot a question. It answers only from the uploaded help docs, and names them.

    Answers are cached in Redis for a day, or until the docs change. The X-Cache header says
    whether this one came from the cache (HIT) or from OpenAI just now (MISS).
    """
    if bot is None:
        raise HTTPException(status_code=503, detail="The bot isn't set up: OPENAI_API_KEY is empty")
    try:
        answer = answer_with_cache(db, redis, bot, body.question)
    except OpenAIError:
        # A timeout, an outage, or a spent budget. None of those is the asker's fault, so it's
        # a 503 to try again later, not a 500.
        logger.exception("OpenAI couldn't answer a question")
        raise HTTPException(status_code=503, detail="The bot can't answer right now. Try again.")
    response.headers["X-Cache"] = "HIT" if answer.cached else "MISS"
    return AskResponse(answer=answer.text, sources=answer.sources)
