# Support AI

An AI customer support platform. It starts as a ticket API built with FastAPI and SQLAlchemy, and grows into a WhatsApp bot that answers from your documents and triages tickets with machine learning.

The build plan is in [docs/ROADMAP.md](docs/ROADMAP.md), and each finished stage is summarized in [docs/stages/](docs/stages/).

## Run it locally

You need Python 3.14, [uv](https://docs.astral.sh/uv/), PostgreSQL 16, and Redis. On a Mac, install and start them with Homebrew, then create a user and a database for the app:

```bash
brew install postgresql@16
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc   # then open a new terminal
brew services start postgresql@16        # runs in the background, and starts again at login
createuser --pwprompt support            # asks you for a password
createdb --owner support support
createdb --owner support support_test    # the tests' own database, which they empty
brew install redis
brew services start redis
```

Copy `.env.example` to `.env`, and put that password in `DATABASE_URL` and `TEST_DATABASE_URL`. The Redis URLs work as they are. Settings are read from `.env`, which git ignores, and a real environment variable with the same name wins over it.

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head              # create the tables
uv run uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs to try the API.

Every schema change is an Alembic migration in `alembic/versions/`. After pulling new code, run `uv run alembic upgrade head` again. See [alembic/README](alembic/README) for the other migration commands.

Other commands:

```bash
uv run pytest                            # run the tests on support_test, never the app's database
uv run python -m scripts.seed            # add 1,000 realistic fake tickets
uv run python -m scripts.weekly_report   # save a weekly report to reports/weekly_report.csv
uv run python -m scripts.explain_list    # time the list query with EXPLAIN ANALYZE (seed 100,000 first)
uv run python -m scripts.measure_cache   # time GET /stats with and without the cache (API running)
```

## API

| Method | Path | What it does | Success |
| --- | --- | --- | --- |
| `GET` | `/tickets` | List tickets, newest first, with filters and pagination | 200 |
| `POST` | `/tickets` | Open a ticket | 201 |
| `GET` | `/tickets/{ticket_id}` | Get one ticket | 200 |
| `GET` | `/tickets/{ticket_id}/messages` | Get a ticket's conversation, oldest message first | 200 |
| `PATCH` | `/tickets/{ticket_id}` | Change a ticket's status, priority, or both | 200 |
| `GET` | `/stats` | Ticket counts for the dashboard, cached for 60 seconds | 200 |

### A ticket

```json
{
  "id": 1,
  "customer": {"id": 1, "name": "Jane Doe", "phone": "+16145550123"},
  "agent_id": null,
  "subject": "Order late",
  "status": "open",
  "priority": "medium",
  "created_at": "2026-10-18T21:35:49.123456Z",
  "closed_at": null
}
```

- `customer` is who opened the ticket. A phone number belongs to one customer.
- `agent_id` is the agent handling the ticket, or `null` until someone picks it up.
- `status` is `open`, `pending`, or `closed`. New tickets start `open`.
- `priority` is `low`, `medium`, `high`, or `urgent`. New tickets start `medium`.
- The server sets `created_at`. It sets `closed_at` when the ticket closes, and clears it if the ticket reopens.
- Times are UTC, marked with a `Z`.

### Open a ticket

```bash
curl -X POST http://127.0.0.1:8000/tickets \
  -H "Content-Type: application/json" \
  -d '{"customer_name": "Jane Doe", "phone": "+16145550123", "subject": "Order late", "message": "Where is my order?"}'
```

Returns `201 Created` with the new ticket. The server finds the customer by phone number, or adds them if the number is new. A returning customer keeps the name they gave first. The message becomes the first one in the ticket's conversation. All four fields are required:

| Field | Rule |
| --- | --- |
| `customer_name` | 1 to 100 characters, after trimming spaces from both ends |
| `phone` | E.164 format: a `+`, then 2 to 15 digits, not starting with 0, like `+16145550123` |
| `subject` | 1 to 200 characters, after trimming spaces |
| `message` | 1 to 5,000 characters, after trimming spaces |

Any other field, like `status`, gets a 422, because customers can't set it. Text can't contain the NUL character (`\u0000`).

### List tickets

```bash
curl "http://127.0.0.1:8000/tickets?status=open&priority=urgent&skip=0&limit=20"
```

| Query parameter | Default | Rule |
| --- | --- | --- |
| `status` | any | `open`, `pending`, or `closed` |
| `priority` | any | `low`, `medium`, `high`, or `urgent` |
| `skip` | `0` | How many tickets to pass over: 0 to 2,147,483,647 |
| `limit` | `20` | How many tickets to return: 1 to 100 |

**Pagination:** results are sorted newest first, by `created_at` and then by `id`, so tickets created in the same second keep a fixed order between requests. To page through, keep `limit` the same and add it to `skip` each time: `skip=0`, `skip=20`, `skip=40`, and so on. A page with fewer than `limit` tickets is the last one, and a page past the end is an empty list, `[]`.

### Get one ticket

```bash
curl http://127.0.0.1:8000/tickets/1
```

### Get a ticket's messages

```bash
curl http://127.0.0.1:8000/tickets/1/messages
```

```json
[
  {"id": 1, "sender": "customer", "body": "Where is my order?", "created_at": "2026-10-18T21:35:49.123456Z"},
  {"id": 2, "sender": "agent", "body": "It ships today.", "created_at": "2026-10-18T22:10:02.402114Z"}
]
```

`sender` is `customer`, `agent`, or `bot` (the WhatsApp bot, from Stage 6). A ticket that doesn't exist gets a 404.

### Update a ticket

```bash
curl -X PATCH http://127.0.0.1:8000/tickets/1 \
  -H "Content-Type: application/json" \
  -d '{"status": "closed"}'
```

Send `status`, `priority`, or both. A body with neither, or with any other field, gets a 422. Closing a ticket sets `closed_at`, closing it again keeps the first time, and reopening it (to `open` or `pending`) clears it. Sending the same update twice has the same effect as sending it once.

### Ticket counts

```bash
curl -i http://127.0.0.1:8000/stats
```

```json
{
  "by_status": {"open": 4701, "pending": 2546, "closed": 92753},
  "backlog_by_priority": {"low": 2768, "medium": 3069, "high": 1030, "urgent": 380}
}
```

`by_status` counts every ticket. `backlog_by_priority` counts the tickets not closed yet (open or pending), which is the work still waiting.

The counts are cached in Redis for 60 seconds, and the cache is cleared whenever a ticket is created or updated, so they're always current. The `X-Cache` header says `HIT` if they came from the cache, or `MISS` if they were just counted in the database. If Redis is down, the API counts in the database every time instead of failing.

### Status codes

| Code | When |
| --- | --- |
| 200 OK | A list, a ticket, or an updated ticket |
| 201 Created | A new ticket was opened |
| 404 Not Found | No ticket has that id |
| 422 Unprocessable Content | The input breaks a rule: a missing, unknown, or invalid field, or an id, `skip`, or `limit` out of range |

A 404 returns:

```json
{"detail": "Ticket not found"}
```

A 422 returns FastAPI's standard list of errors. `loc` says where the problem is, and `msg` says what's wrong:

```json
{
  "detail": [
    {
      "type": "string_pattern_mismatch",
      "loc": ["body", "phone"],
      "msg": "String should match pattern '^\\+[1-9][0-9]{1,14}$'",
      "input": "614-555-0123",
      "ctx": {"pattern": "^\\+[1-9][0-9]{1,14}$"}
    }
  ]
}
```

Ticket ids run from 1 to 2,147,483,647, the largest value a PostgreSQL integer column can hold. An id outside that range gets a 422, and an id in range with no ticket gets a 404. No input should ever cause a 500.

## Project layout

- `app/`: the API. `main.py` has the endpoints, `models.py` the database tables, `schemas.py` the request and response shapes, `database.py` the database connection, `cache.py` the Redis cache, and `config.py` the settings.
- `alembic/`: database migrations.
- `scripts/`: the seed script and the weekly report.
- `tests/`: pytest tests.
- `docs/`: the roadmap, stage summaries, and [system design notes](docs/system-design/).
