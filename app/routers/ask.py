"""POST /ask: put a question to the bot, which answers from the help docs (app/bot.py).

Customers reach the bot through WhatsApp. This endpoint lets agents try it out, and is how the
bot's accuracy is measured.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from openai import OpenAIError

from app.bot import answer_question, get_chat_model
from app.config import settings
from app.database import SessionDep
from app.embeddings import get_embeddings
from app.schemas import AskRequest, AskResponse, ErrorResponse
from app.security import get_current_agent

logger = logging.getLogger(__name__)

NOT_SET_UP = "The bot isn't set up: OPENAI_API_KEY is empty"


def bot_embeddings() -> Embeddings:
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail=NOT_SET_UP)
    return get_embeddings()


def bot_chat_model() -> BaseChatModel:
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail=NOT_SET_UP)
    return get_chat_model()


# The tests swap these for LangChain's fakes, so no test pays OpenAI.
EmbeddingsDep = Annotated[Embeddings, Depends(bot_embeddings)]
ChatModelDep = Annotated[BaseChatModel, Depends(bot_chat_model)]

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
def ask(body: AskRequest, db: SessionDep, embeddings: EmbeddingsDep, llm: ChatModelDep):
    """Ask the bot a question. It answers only from the uploaded help docs, and names them."""
    try:
        answer = answer_question(db, embeddings, llm, body.question)
    except OpenAIError:
        # A timeout, an outage, or a spent budget. None of those is the asker's fault, so it's
        # a 503 to try again later, not a 500.
        logger.exception("OpenAI couldn't answer a question")
        raise HTTPException(status_code=503, detail="The bot can't answer right now. Try again.")
    return AskResponse(answer=answer.text, sources=answer.sources)
