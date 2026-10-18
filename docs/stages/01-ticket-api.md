# Stage 1: Ticket API with FastAPI and Pandas

Done when: a tested ticket API runs locally on SQLite, and a Pandas script produces a weekly report. Both are true. 128 tests pass, the API was exercised with curl against a seeded database, and the report prints and saves 12 weeks of numbers.

## What was built

- **A ticket API** (`app/`) with four endpoints: open a ticket, list tickets with filters and pagination, get one ticket, and change a ticket's status or priority. Tickets are stored in SQLite through SQLAlchemy 2.x.
- **Input validation** (`app/schemas.py`): text lengths checked after trimming spaces, E.164 phone numbers, unknown fields rejected, a PATCH that must send status or priority, and ids and `skip` capped at `MAX_INT`. A missing ticket returns 404, and bad input returns FastAPI's standard 422.
- **128 pytest tests** (`tests/`): 91 for validation rules and boundary cases, 20 for endpoint behavior, 9 for the seed script, and 8 for the report. Each test gets a fresh in-memory database.
- **A seed script** (`scripts/seed.py`) that makes 1,000 realistic fake tickets over the last 12 weeks.
- **A weekly report** (`scripts/weekly_report.py`) that uses Pandas to compute weekly volume, average time to close, and open tickets by priority, saved as a CSV.
- **Docs**: the API section of the README, and system design notes on [how a request reaches the API](../system-design/01-request-journey.md) and [REST API design](../system-design/02-rest-api-design.md).

## Key decisions and why

**Separate create, read, and update models.** `TicketCreate` has every input rule. `TicketRead` has none, so any row in the database can be returned, even one written before a rule existed or by a script. A test saves a row that breaks today's rules and checks that the API still returns it. `TicketUpdate` only allows status and priority, because those are the only things an agent changes.

**`extra="forbid"`.** Without it, Pydantic quietly ignores unknown fields. A customer sending `"status": "closed"` would get a 201 and believe it worked. Rejecting the field makes the mistake visible.

**Trim spaces before checking lengths.** Otherwise `"   "` would pass the 1-character minimum, and a blank name would be saved.

**`[0-9]` instead of `\d` for phone numbers.** In Python regular expressions, `\d` also matches digits from other alphabets, such as Arabic-Indic `١٢٣`. A test sends exactly that.

**Cap ids and `skip` at `MAX_INT` (2,147,483,647).** That's the largest value a PostgreSQL integer can hold. A bigger number gets a clear 422 instead of reaching the database, where numbers out of its range crash the query.

**A custom 422 handler.** Fuzzing the API found five inputs that caused a 500: `NaN`, `Infinity`, `1e999`, half of an emoji (`"\ud800"`, as a value or a key), and lists nested about 1,000 deep. Pydantic rejected all of them correctly. The crash came afterwards, because FastAPI's standard 422 repeats the bad input, and Python can read these inputs but can't write them back out as JSON. The handler returns the standard response, and leaves out the repeated input only when it can't be written. With the handler removed, all six of those test cases fail, so the tests guard the fix.

**Reject the NUL character in text.** SQLite stores it, but PostgreSQL refuses it, so it would become a 500 in Stage 2. No real name or message contains one.

**Create tables when the server starts, not on import.** `Base.metadata.create_all` used to run when `main.py` was imported. Importing the app in tests then created `support.db`. It now runs in FastAPI's lifespan, at server startup, and Stage 2 replaces it with Alembic migrations.

**Sort by `created_at`, then `id`.** SQLite's timestamps are only accurate to the second, so tickets created together tie. Without a unique tie-breaker, their order could change between requests, and pagination could repeat or skip a ticket.

**Test with an in-memory database and dependency overrides.** `app.dependency_overrides[get_db]` points each request at a fresh in-memory SQLite database. `StaticPool` keeps a single connection open, because an in-memory database disappears when its last connection closes. Tests never open `support.db`.

**Seed data that behaves like real data.** Time to close follows a log-normal distribution with a typical time set by priority (3 hours for urgent, 60 for low). Most tickets close near that time, and a few take much longer. A ticket is closed only if its close time has passed, and 5% stall, so old tickets are mostly closed and this week's mostly open. Every fake ticket passes through `TicketCreate`, and phone numbers use 555-0100 to 555-0199, which are set aside for fiction.

**Report definitions.** Average time to close groups tickets by the week they *closed*. Grouping by the week they opened would make recent weeks look fast, since only their quick tickets have closed so far. "Open at the end of the week" means not yet closed, so it includes pending tickets, because the database doesn't keep status history.

**Dev dependencies.** pytest, httpx2, Faker, and Pandas are in uv's `dev` group. The API never imports them, so the Stage 4 Docker image can leave them out with `uv sync --no-dev`. httpx2 is there because Starlette's TestClient now prefers it over httpx.

## How to run and demo it

```bash
uv sync
uv run pytest                            # 128 tests, under a second
uv run python -m scripts.seed            # 1,000 fake tickets in support.db
uv run uvicorn app.main:app --reload     # then open http://127.0.0.1:8000/docs
uv run python -m scripts.weekly_report   # prints the table, saves reports/weekly_report.csv
```

A five-minute demo in `/docs`:

1. **List** open urgent tickets with `status=open&priority=urgent`, then page with `skip` and `limit`.
2. **Create** a ticket with the example body (201), then fetch it by id.
3. **Break a rule:** send phone `614-555-0123`, or a name of only spaces, and show the 422 pointing at the field.
4. **Close** the ticket with PATCH `{"status": "closed"}` and show `closed_at`. Send `{}` and show the 422, "Send status, priority, or both."
5. **Get** ticket `2147483647` (404), then `2147483648` (422).
6. **Run the report** and talk through a row: tickets created, tickets closed, average hours to close, and the open backlog by priority.

Real output from the seeded data (your numbers will differ, since the seed is random):

```text
week_start  tickets_created  tickets_closed  avg_hours_to_close  open_low  open_medium  open_high  open_urgent  open_total
2026-09-28               64              59                49.4        22           30          9            3          64
2026-10-05               81              75                33.3        25           31         10            4          70
2026-10-12               84              78                50.3        23           37         12            4          76
```

The backlog grows week over week because 5% of seeded tickets stall and never close. Spotting a pile-up like that is exactly what a weekly report is for.

## What an interviewer might ask

- **Why three Pydantic models for one ticket?** Input and output have different jobs. Rules belong on input, and output must be able to show every stored row. A separate create model also keeps clients from setting fields the server owns, like `id` and `created_at`.
- **What's the difference between a 404 and a 422 here?** A 422 means the request itself is invalid, and it's caught before any database work. A 404 means the request is valid but the ticket doesn't exist.
- **How did you make sure no input causes a 500?** Rules at the edges (lengths, patterns, numeric ranges), then fuzzing with hostile input. That found the 422 handler crash, now fixed and covered by tests that fail without the fix.
- **Why is your PATCH idempotent, and why does that matter?** It sets values instead of changing them relative to what's there, and closing twice keeps the first `closed_at`. A client that retries after a timeout can't make things worse.
- **How does your pagination work, and what breaks at scale?** Offset and limit, sorted by `created_at` and `id`. Deep offsets get slow, and pages shift when new tickets arrive. Cursor pagination fixes both (see the REST notes).
- **How do the tests avoid touching real data?** Dependency injection: `get_db` is overridden with a session on an in-memory database, and the app no longer creates tables on import.
- **How did you make fake data realistic, and why does it matter?** Log-normal close times by priority, closing only tickets whose close time has passed, and a stall rate. Without that, the report would show meaningless numbers.
- **Why group average time to close by close week?** To avoid survivorship bias in recent weeks.

## Known limitations, for later stages

- **Times show without a timezone.** SQLite drops it, and every time is stored in UTC. PostgreSQL in Stage 2 keeps it, and the tests already read times as UTC either way.
- **No request size limit.** A huge body is read in full. The proxy in Stage 5 (Caddy) should cap body size. A body nested about a million levels deep gets FastAPI's own 400, not a 500.
- **No auth yet.** Anyone can list and update tickets until Stage 3 adds JWT login.
- **Offset pagination** will be measured on 100,000 tickets in Stage 2.

## A note on the development environment

The repo lives in iCloud Drive, and macOS had moved most `.venv` files to the cloud to save space. Every import then waited for a download, and Python hung. The virtual environment now lives in `.venv.nosync`, a name iCloud doesn't sync, and `.venv` is a symlink to it, so `uv sync` and `uv run` work as before. If the environment ever breaks, rebuild it with:

```bash
rm -rf .venv .venv.nosync
UV_PROJECT_ENVIRONMENT=.venv.nosync uv sync
ln -s .venv.nosync .venv
```

The better long-term fix is to move the repo out of iCloud Drive, for example to `~/code/support-ai`. iCloud can also offload or duplicate files inside `.git`. GitHub is already the backup, so iCloud adds risk without adding safety.
