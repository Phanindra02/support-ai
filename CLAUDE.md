# Support AI

An AI customer support platform, built as a portfolio project for software and AI engineering jobs. Customers ask questions on WhatsApp, and a bot answers from uploaded help docs. ML models set ticket priority and flag angry customers, and urgent tickets alert the team in Slack through n8n.

The full plan and its progress live in `docs/ROADMAP.md`. Read it before starting work, and tick each step there in the same commit that finishes it.

## Who this is for

The developer is preparing for interviews and must be able to explain every part of this project. Prefer clear, conventional solutions over clever ones, and explain decisions in plain language in commit messages and stage summaries.

## Stack

- Python 3.14, managed with uv (`pyproject.toml` and `uv.lock`, no requirements.txt). If a library or AWS Lambda doesn't support 3.14 yet, use the newest version that does for that one component and note why.
- FastAPI, Pydantic v2, and SQLAlchemy 2.x.
- SQLite today. PostgreSQL from Stage 2 (driver: psycopg 3, URL scheme `postgresql+psycopg://`), with pgvector from Stage 6. Redis from Stage 2.
- Later stages add Alembic, RQ, Twilio WhatsApp, n8n, Docker, GitHub Actions, AWS (Graviton EC2, RDS, S3, Lambda, ECR, Parameter Store), LangChain with OpenAI, scikit-learn and XGBoost, and Kubernetes (minikube, EKS, Helm).
- The developer uses an Apple Silicon Mac and installs tools with Homebrew. The repo is `Phanindra02/support-ai` on GitHub, and the gh CLI is installed and logged in.

## Commands

```bash
uv sync                                  # install dependencies
uv run uvicorn app.main:app --reload     # run the API; docs at http://127.0.0.1:8000/docs
uv run pytest                            # run the tests
uv add <package>                         # add a dependency (never pip install)
```

## Layout

- `app/`: the API. `database.py` (engine, SessionLocal, Base, get_db), `models.py` (SQLAlchemy models), `schemas.py` (Pydantic schemas), `main.py` (creates the app and includes the routers), and `routers/` (the endpoints, one file per area).
- `worker/`: the RQ background worker (Stage 6).
- `ml/`: model training and the model service (Stage 7).
- `lambda/`: the S3-triggered PDF text extraction Lambda (Stage 5).
- `infra/`: Dockerfiles, Kubernetes manifests, the Helm chart, exported n8n workflows, and the branch protection rules for `main` (`infra/github/`).
- `docker-compose.yml`: at the root rather than in `infra/`, so `docker compose up` finds it and the `.env` beside it with no flags.
- `scripts/`: the seed script and the Pandas weekly report.
- `tests/`: pytest tests.
- `docs/`: the roadmap, stage summaries (`docs/stages/`), system design notes (`docs/system-design/`), measured numbers (`docs/results.md`), and the design doc.

## Code conventions

- SQLAlchemy 2.x typed style only: `DeclarativeBase`, `Mapped`, `mapped_column`, `select()`, and `session.get()`. Never `declarative_base()`, `Column()`, or `session.query()`.
- Enum columns use `StrEnum` with lowercase member names and `Enum(..., validate_strings=True)`.
- Datetimes are timezone-aware UTC: `DateTime(timezone=True)` and `datetime.now(timezone.utc)`.
- Use separate Pydantic models for create, read, and update. Input rules (lengths, patterns, `extra="forbid"`) go on create and update models only. Read models carry no input rules, so every stored row can be returned.
- A missing resource returns 404 with `{"detail": "..."}`, and bad input returns FastAPI's standard 422. No input may cause a 500. Path ids and `skip` are capped at `MAX_INT` (2,147,483,647).
- Phone numbers are E.164 strings, like `+16145550123`.
- When the first setting arrives (Stage 2's database URL), add `app/config.py` with pydantic-settings, read from a git-ignored `.env`, and commit a `.env.example` with placeholder values.
- Keep comments short and plain, and explain why rather than what.

## How to work

- Follow `docs/ROADMAP.md` in order, one stage at a time. Meet a stage's "Done when" before starting the next.
- Every step ships with pytest tests, and `uv run pytest` must pass before each commit.
- Commit after each roadmap step, with the step ticked in `docs/ROADMAP.md`. Push when the stage is done. Once Stage 4 protects `main`, work on a branch and open a pull request with gh instead.
- Record every measured number (query timings before and after indexes, cache latency, model F1, retrieval accuracy, load-test results) in `docs/results.md`, along with how it was measured.
- For each system design "Learn" item, write a short notes file in `docs/system-design/` that explains the topic as it applies to this project. Do each "Apply it" item in code or docs.
- When a stage is done, write `docs/stages/NN-name.md`: what was built, the key decisions and why, how to run and demo it, and what an interviewer might ask about it. Then stop and summarize for the developer before starting the next stage.

## Stop and ask first

- Any step marked **[You]** in the roadmap, or anything else that needs the developer's accounts, keys, or payment: AWS, Twilio, OpenAI, Kaggle, Slack, ngrok, or a domain registrar. Prepare everything around it, then give exact instructions. Have the developer enter secrets themselves rather than paste them into chat.
- Creating any billable cloud resource. Give the expected monthly cost first, and ask which AWS region to use before Stage 5.
- Deleting data, force-pushing, or rewriting git history.

## Never

- Commit `.env`, keys, tokens, or database files, or print secrets in logs or output.
- Leave the EKS cluster running after the Stage 8 demo is recorded.
- Do the developer's own practice for them: Blind 75 problems, mock interviews, and timed system design practice.
