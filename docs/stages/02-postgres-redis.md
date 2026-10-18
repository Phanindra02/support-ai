# Stage 2: PostgreSQL and Redis

Done when: the API runs on Postgres with versioned migrations, measured index speedups, and Redis caching and rate limiting. All four are true:

- The API runs on PostgreSQL 16, and its schema comes from three Alembic migrations.
- Indexes made the first page of the ticket list about 550 times faster on 100,000 tickets (11.07 ms to 0.02 ms).
- `GET /stats` is cached in Redis, about 1 ms on a hit against 6 ms on a miss.
- Each IP is limited to 60 requests a minute.
- 197 tests pass on a separate Postgres test database and Redis database.

## What was built

- **PostgreSQL** (Homebrew, background service) with its own `support` user and database. Settings come from a git-ignored `.env` through `app/config.py` (pydantic-settings), with `.env.example` committed.
- **Alembic migrations** (`alembic/versions/`) replace `create_all`:
  1. Create the tickets table.
  2. Add customers, agents, and messages, moving existing data across.
  3. Index tickets by created time and status.
- **A four-table schema:**
  - **customers:** unique phone numbers.
  - **agents:** logins come in Stage 3.
  - **tickets:** each linked to a customer, and to an agent once assigned.
  - **messages:** the conversation on each ticket.
- **API changes.** A ticket now returns a nested `customer`. `GET /tickets/{id}/messages` returns the conversation, and `GET /stats` returns cached counts. `POST /tickets` still takes the same body.
- **A bigger seed** (`scripts/seed.py`). It now makes agents, returning customers, and conversations, and can make 100,000 tickets in 30 seconds.
- **Measuring scripts.** `scripts/explain_list.py` times the list query with `EXPLAIN ANALYZE`. `scripts/measure_cache.py` times `/stats` on a cache hit and a miss.
- **A Redis cache** (`app/cache.py`) for `/stats`, cleared whenever a ticket changes.
- **A rate limit** (`app/rate_limit.py`): a sliding window log in Redis, 60 requests a minute per IP.
- **Docs:**
  - [results.md](../results.md), with every number and how it was measured.
  - Notes on [how databases scale](../system-design/03-database-scaling.md).
  - Notes on [caching and rate limiting](../system-design/04-caching-and-rate-limiting.md), including what this project uses and why.

## Key decisions and why

**Settings with no defaults for URLs.** `DATABASE_URL` and `REDIS_URL` must be set. A missing one stops the app at startup with a clear error, instead of quietly connecting somewhere else. A test fails if `.env.example` falls out of step with the settings.

**Every connection asks Postgres for UTC.** Postgres hands back times in the server's time zone, New York on this Mac, so the API would have returned `14:00-04:00` instead of `18:00Z`. The engine sets `timezone=utc` on each connection.

**Constraint naming rules.** `Base.metadata` names every constraint and index predictably (`fk_tickets_customer_id_customers`). A later migration has to name a constraint to drop or change it.

**Autogenerate is a first draft.** It got two things wrong, and both were fixed by hand:
- Dropping the tickets table left its Postgres enum types behind, so downgrading and then upgrading failed with "type already exists".
- For the customers migration, it added a required column to a table that already had rows, and dropped three columns of data.

That migration was rewritten as **expand, backfill, contract**:
1. Add the new tables, with a nullable `customer_id`.
2. Copy the data: one customer per phone, and each ticket's text as its first message.
3. Make `customer_id` required, link the tables, and drop the old columns.

The local database went through it with all 1,000 tickets intact.

**Migrations are tested.** On a real Postgres database, tests check that:
- Upgrading builds exactly the schema the models describe.
- Downgrading removes every table and type.
- Each migration can be undone and redone.
- The data migration moves real rows correctly in both directions.

**Tests on Postgres, emptied after each test.** The roadmap listed this step last. It was done third, because the steps after it rely on behavior SQLAlchemy's SQLite setup doesn't reproduce: savepoints, unique-constraint races, and data migrations.
- **Emptied, not rolled back.** Rolling back one transaction per test is faster, but inside a transaction Postgres's `now()` stays frozen at the moment it began, which broke the `created_at` tests. Emptying every table with `TRUNCATE` keeps the whole suite at about 4 seconds.
- **A guard.** The tests refuse any database whose name doesn't end in `_test`, before anything is dropped.

**A savepoint makes find-or-create safe under a race.** Two first messages from the same new number can both miss the lookup and both insert the customer. The unique constraint on phone rejects the second insert. The savepoint (`db.begin_nested()`) undoes just that insert, so the request continues with the existing customer. Without the savepoint, the error spoils the whole transaction (`PendingRollbackError`). A test forces this race, and it fails without the savepoint.

**No N+1 queries.** The list loads its tickets' customers with `selectinload`: one query for the page and one for all its customers, instead of one more per ticket. A test counts the SQL statements (2, not 21).

**Index the sort order, and keep only indexes that are used:**
- **`(created_at, id)`** matches `ORDER BY created_at DESC, id DESC` exactly. Postgres reads it backward and stops after 20 rows, with no sort.
- **status** is kept, because the stats counts use it.
- **priority** was added and measured, and the planner never used it (0 scans in `pg_stat_user_indexes`), so it was left out. Every index slows every write and takes space.

A test checks that the first page is read from the index with no sort step, and fails if the sort order stops matching the index.

**Cache-aside, deleted after commit, with a 60-second expiry as a safety net.**
- **Why cache-aside.** The counts cover every ticket, so recounting on every write (write-through) would slow the path that matters for a read nobody may make.
- **Why delete after the commit.** Deleting before would let another request cache the old counts again before the new ticket is saved.
- **Why still expire.** The expiry limits how long a missed or failed delete can leave counts wrong.

**Redis must never take the API down.** The tests found that redis-py retries a failed command 3 times with growing waits by default, so while Redis was down each cache call took 2.7 seconds. The client now has retries off and half-second timeouts. With Redis down, `/stats` counts in Postgres, the rate limit lets requests through, and tickets can still change. A test checks the request takes under half a second.

**A sliding window log for the rate limit.** It keeps the promise "60 in any 60 seconds" exactly. A fixed window allows 120 in two seconds around the minute, and a token bucket is built to allow bursts. The log's cost, at most 60 small entries per IP, doesn't matter at this limit. Other details:
- **One step.** The remove, add, and count run in one `MULTI`/`EXEC` transaction, so two requests at once can't both get in as the 60th.
- **Rejected requests don't count.** A rejected request removes its own entry, so a client that waits for `Retry-After` gets its full allowance back.

**The seed runs in batches inside one transaction.** Batches of 5,000 keep memory flat (249 MB peak for 100,000 tickets), and one transaction means a failed run adds nothing. Tickets are made oldest first, so ids rise with time, and each customer joins when they open their first ticket.

## How to run and demo it

```bash
brew services start postgresql@16 && brew services start redis   # if not already running
uv sync
uv run alembic upgrade head
uv run pytest                                                     # 197 tests, about 4 seconds
uv run python -m scripts.seed --reset --count 100000 --seed 42    # 30 seconds
uv run uvicorn app.main:app --reload                              # http://127.0.0.1:8000/docs
```

A ten-minute demo:

1. **Migrations.** Run `uv run alembic history`, then open the customers migration and walk through expand, backfill, contract.
2. **Indexes, live.** Remove the indexes, measure, then put them back and measure again:
   ```bash
   uv run alembic downgrade -1 && uv run python -m scripts.explain_list
   uv run alembic upgrade head && uv run python -m scripts.explain_list
   ```
   The first page goes from about 11 ms to 0.02 ms. Run with `--plans` to show the sequential scan turn into a backward index scan.
3. **Customers and messages.** In `/docs`, open two tickets with the same phone number and different names. Both point to the same customer, who keeps the first name. Then call `GET /tickets/{id}/messages`.
4. **The cache.** Run `curl -i localhost:8000/stats` twice: `X-Cache: MISS`, then `HIT`. Open a ticket, and the next call is a `MISS` with the new count.
5. **The rate limit.** Send 61 requests, and the last one gets a 429 with `Retry-After`:
   ```bash
   for i in $(seq 61); do curl -s -o /dev/null -w '%{http_code} ' 'localhost:8000/tickets?limit=1'; done
   ```
6. **Redis down.** Run `brew services stop redis`. `/stats` still answers (always `MISS`), and nothing is rate limited. Start it again with `brew services start redis`.

## What an interviewer might ask

- **Why migrations instead of `create_all`?** `create_all` only creates missing tables. It can't add a column, rename one, or move data. Migrations are versioned, reviewed like code, applied the same way everywhere, and can be undone.
- **How did you split one table into four without losing data?** Expand, backfill, contract, in one migration. Postgres runs schema changes inside transactions, so a failure halfway leaves nothing behind. Tests run it on real rows in both directions.
- **What happens if two requests create the same new customer at once?** Covered above: the unique constraint catches it, and a savepoint lets the request recover. Under Postgres's default isolation (read committed), the second lookup sees the other request's committed row.
- **What's the N+1 problem?** Loading a list, then running one more query per item for related data. Here that would be 21 queries for a 20-ticket page. `selectinload` makes it 2, and a test enforces it.
- **How did you choose indexes?** Measured first. `(created_at, id)` matches the sort, so the first page needs no sort. Priority was measured and dropped, because the planner never used it.
- **Why didn't the deep page get much faster?** `OFFSET 50000` still reads and throws away 50,000 rows. The index saved the sort, not the walk. Cursor pagination fixes it.
- **Why truncate the test database instead of rolling back?** `now()` is frozen inside a transaction.
- **Why cache-aside and not write-through?** And why delete after the commit? Covered above.
- **What if Redis goes down?** The API keeps working, without the cache and without the rate limit, and logs a warning. Explain the 2.7-second retry problem and its fix.
- **Sliding window log or token bucket?** Exactness at a small limit versus allowing bursts. How it stays correct across many API copies: shared Redis, and one atomic transaction.
- **Why fail open?** Availability over protection for a general API. Login is the exception (Stage 3).

## Known limitations, for later stages

- **Offset pagination** takes 7.6 ms at `skip=50000`. Cursor pagination would make every page as fast as the first.
- **The rate limit is per IP.**
  - Behind Caddy (Stage 5), uvicorn must trust `X-Forwarded-For` from the proxy only.
  - The Twilio webhook (Stage 3) must not share one IP limit for all customers.
  - A production Redis needs `maxmemory` with `volatile-lru`.
- **No agent endpoints yet,** and nothing assigns tickets through the API. Stage 3 adds logins and roles.
- **`GET /tickets/{id}/messages` isn't paginated.** That's fine for support conversations, but a spammer could make one long.
- **The tests need Postgres and Redis running.** Stage 4's CI must start both as service containers.

## Changes made on the developer's Mac

- **Postgres.app is stopped,** and its "start at login" setting is off, so Homebrew's PostgreSQL 16 can use port 5432. Its two databases are untouched in `~/Library/Application Support/Postgres/var-17`. To use it again, open Postgres.app and start the server, after `brew services stop postgresql@16`.
- **PostgreSQL 16 (Homebrew)** was already installed, but its service kept failing because the port was taken. It now runs as a background service. The `support` user and the `support` and `support_test` databases are new. The password was generated and written straight into `.env`.
- **Redis 8 (Homebrew)** is installed and runs as a background service, listening only on localhost.
- **`support.db`,** the old SQLite file, is no longer used. It's git-ignored and can be deleted.
