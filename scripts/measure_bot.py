"""Measure the bot on the 25 questions in docs/help-docs/questions.json.

uv run python -m scripts.measure_bot [--chunk-size 1000] [--chunk-count 4]

It calls OpenAI with the key in .env, for about $0.02 a run. The help docs are split and
embedded here, and the closest chunks found in memory with the same cosine distance pgvector
uses, so trying another chunk size doesn't touch any database. Then the bot's own prompt and
LLM answer, through app/bot.py.

For each question, it checks three things:
- retrieval: one of the chunks found holds the answer's key phrase,
- answer: the bot's reply holds that phrase, or says "I don't know." when the docs don't,
- source: the bot names the doc the answer is in, and names none when it doesn't know.
"""

import argparse
import json

import numpy as np
from pypdf import PdfReader

from app.bot import CHUNK_COUNT, answer_from_chunks, get_bot
from app.config import PROJECT_ROOT
from app.models import Chunk, Document
from worker.jobs import CHUNK_SIZE, split_text

HELP_DOCS = PROJECT_ROOT / "docs" / "help-docs"


def normalize(text: str) -> str:
    """Lowercase with single spaces, since PDF text breaks lines inside sentences."""
    return " ".join(text.split()).lower()


def load_chunks(chunk_size: int) -> list[Chunk]:
    """Split each help doc's PDF the way the Lambda and the worker do, with 20% overlap."""
    chunks = []
    for pdf in sorted(HELP_DOCS.glob("*.pdf")):
        document = Document(filename=pdf.name)
        text = "\n\n".join(page.extract_text() for page in PdfReader(pdf).pages)
        for position, piece in enumerate(split_text(text, chunk_size, chunk_size // 5)):
            chunks.append(Chunk(document=document, position=position, text=piece))
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--chunk-count", type=int, default=CHUNK_COUNT)
    args = parser.parse_args()

    bot = get_bot()
    if bot is None:
        raise SystemExit("Set OPENAI_API_KEY in .env first")
    questions = json.loads((HELP_DOCS / "questions.json").read_text())
    chunks = load_chunks(args.chunk_size)
    vectors = np.array(bot.embeddings.embed_documents([chunk.text for chunk in chunks]))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)

    retrieved = answered = sourced = 0
    answerable = [q for q in questions if q["source"]]
    for q in questions:
        query = np.array(bot.embeddings.embed_query(q["question"]))
        distances = 1 - vectors @ (query / np.linalg.norm(query))
        closest = [chunks[i] for i in np.argsort(distances, kind="stable")[: args.chunk_count]]
        answer = answer_from_chunks(bot.llm, q["question"], closest)
        phrase = normalize(q["answer"])
        if q["source"]:
            found = any(phrase in normalize(chunk.text) for chunk in closest)
            right = phrase in normalize(answer.text)
            named = answer.sources == [q["source"]]
        else:
            found = True  # Nothing to find.
            right = not answer.known
            named = answer.sources == []
        retrieved += found and bool(q["source"])
        answered += right
        sourced += named
        if not (found and right and named):
            print(f"MISS {q['question']!r}: found={found} right={right} sources={answer.sources}")
            print(f"     -> {answer.text!r}")

    total = len(questions)
    print(
        f"chunk size {args.chunk_size}, {len(chunks)} chunks, {args.chunk_count} per question: "
        f"retrieval {retrieved}/{len(answerable)}, answers {answered}/{total}, "
        f"sources {sourced}/{total}"
    )


if __name__ == "__main__":
    main()
