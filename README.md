# Support AI

An AI customer support platform. It starts as a ticket API built with FastAPI and SQLAlchemy, and grows into a WhatsApp bot that answers from your documents and triages tickets with machine learning.

## Run it locally

You need Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs to try the API.
