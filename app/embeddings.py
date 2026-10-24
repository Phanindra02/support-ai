"""Embeddings: OpenAI turns a piece of text into 1,536 numbers that stand for its meaning.

The worker embeds each chunk of a help doc, and POST /ask will embed each question with the
same model, so the two can be compared. Embeddings from different models can't be.
"""

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.config import settings
from app.models import EMBEDDING_DIMENSIONS

# OpenAI's cheapest embedding model, at $0.02 per million tokens, which is plenty to find the
# right paragraph of a help doc.
EMBEDDING_MODEL = "text-embedding-3-small"


def get_embeddings(timeout: float = 30, max_retries: int = 2) -> Embeddings:
    """OpenAI's embeddings, through LangChain. Raises if OPENAI_API_KEY isn't set.

    The worker can wait, so it keeps the defaults. The bot answers while a customer waits,
    so it asks for less (app/bot.py).
    """
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is empty, so nothing can be embedded")
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        api_key=settings.openai_api_key,
        # OpenAI's client waits up to 10 minutes for an answer. A stuck call should fail sooner.
        timeout=timeout,
        max_retries=max_retries,
        # Chunks are about 250 tokens, far under the model's limit of 8,191, so LangChain needn't
        # count them first with tiktoken, which downloads its tables the first time it runs.
        check_embedding_ctx_length=False,
    )
