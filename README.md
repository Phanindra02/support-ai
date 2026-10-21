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

Copy `.env.example` to `.env`, and put that password in `DATABASE_URL` and `TEST_DATABASE_URL`. Set `JWT_SECRET_KEY` to the output of `openssl rand -hex 32`. It's empty in the example on purpose, so the app won't start with a key anyone can read on GitHub. The Redis URLs work as they are. Settings are read from `.env`, which git ignores, and a real environment variable with the same name wins over it.

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head              # create the tables
uv run python -m scripts.create_agent --email you@example.com --name "Your Name" --admin   # asks for a password
uv run uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs to try the API.

Every schema change is an Alembic migration in `alembic/versions/`. After pulling new code, run `uv run alembic upgrade head` again. See [alembic/README](alembic/README) for the other migration commands.

Other commands:

```bash
uv run pytest                            # run the tests on support_test, never the app's database
uv run python -m scripts.seed            # add 1,000 realistic fake tickets (--reset deletes every agent too)
uv run python -m scripts.weekly_report   # save a weekly report to reports/weekly_report.csv
uv run python -m scripts.explain_list    # time the list query with EXPLAIN ANALYZE (seed 100,000 first)
uv run python -m scripts.measure_cache   # time GET /stats with and without the cache (see the script)
```

## API

| Method | Path | What it does | Who | Success |
| --- | --- | --- | --- | --- |
| `POST` | `/auth/token` | Log in as an agent and get an access token | Anyone | 200 |
| `GET` | `/agents/me` | The agent who is logged in | Agents | 200 |
| `GET` | `/agents` | List every agent | Admins | 200 |
| `POST` | `/agents` | Add an agent | Admins | 201 |
| `PATCH` | `/agents/{agent_id}` | Change an agent's name, password, role, or active flag | Admins | 200 |
| `GET` | `/tickets` | List tickets, newest first, with filters and pagination | Agents | 200 |
| `POST` | `/tickets` | Open a ticket for a customer | Agents | 201 |
| `GET` | `/tickets/{ticket_id}` | Get one ticket | Agents | 200 |
| `GET` | `/tickets/{ticket_id}/messages` | Get a ticket's conversation, oldest message first | Agents | 200 |
| `PATCH` | `/tickets/{ticket_id}` | Change a ticket's status, priority, or both | Agents | 200 |
| `GET` | `/stats` | Ticket counts for the dashboard, cached for 60 seconds | Agents | 200 |
| `POST` | `/webhooks/twilio` | Receive a customer's WhatsApp message | Twilio | 200 |

"Agents" means any logged-in agent, admins included. Every endpoint except logging in and the Twilio webhook needs the token described next. The curl examples below leave the header out to stay short.

### Log in

Agents log in with their email and password, and get back a signed access token (a JWT) that works for 30 minutes:

```bash
curl -X POST http://127.0.0.1:8000/auth/token \
  -d "username=you@example.com" --data-urlencode "password=your password"
```

```json
{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "bearer"}
```

The body is a form, not JSON, and the email goes in the `username` field, because that's what the OAuth2 standard says. A wrong email or password gets a `401` with `{"detail": "Incorrect email or password"}`, the same either way, so it doesn't reveal which emails have accounts.

Send the token on later requests in the `Authorization` header:

```bash
curl http://127.0.0.1:8000/agents/me -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..."
```

A missing, expired, or tampered token gets a `401`, and so does a token for an agent who has been deactivated since logging in. In `/docs`, the **Authorize** button logs in and sends the token for you.

Passwords are stored only as Argon2 hashes, and need 15 to 128 characters. The seed script's agents have no password until one is set, so they can't log in.

### Roles and agent management

Each agent is an `agent` or an `admin`. Agents read and update tickets. Admins can do that too, and they also manage agents. An agent who calls an admin endpoint gets a `403`.

The first admin comes from the command line, because only an admin can add agents through the API. The script asks for the password, so it never lands in your shell history:

```bash
uv run python -m scripts.create_agent --email you@example.com --name "Your Name" --admin
```

Run it again with the same email to set a new password. After that, admins manage agents through the API:

```bash
curl -X POST http://127.0.0.1:8000/agents -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Sam Lee", "email": "sam.lee@example.com", "password": "correct horse battery staple"}'

curl -X PATCH http://127.0.0.1:8000/agents/2 -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"is_active": false}'
```

- `role` is `agent` unless you send `"admin"`. Emails are stored lowercase, and a taken email gets a `409`.
- A `PATCH` can change `name`, `password`, `role`, and `is_active`, in any mix. The email can't change, because it's the login.
- Setting `is_active` to `false` locks the agent out at once, even with a token that hasn't expired, because every request looks the agent up. Their tickets keep pointing to them.
- Admins can't demote or deactivate themselves (`403`), so at least one admin always remains.

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

Agents use this to open a ticket for a customer, such as after a phone call. Customers open their own tickets through WhatsApp. Returns `201 Created` with the new ticket. The server finds the customer by phone number, or adds them if the number is new. A returning customer keeps the name they gave first. The message becomes the first one in the ticket's conversation. All four fields are required:

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

Send `status`, `priority`, or both. A body with neither, or with any other field, gets a 422. Changing a ticket's priority to `urgent` also posts it to the team's Slack channel (see [Urgent ticket alerts](#urgent-ticket-alerts)). Closing a ticket sets `closed_at`, closing it again keeps the first time, and reopening it (to `open` or `pending`) clears it. Sending the same update twice has the same effect as sending it once.

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

### Rate limit

Each IP address may make 60 requests in any 60 seconds, to every endpoint together. Request number 61 gets a `429 Too Many Requests`, with a `Retry-After` header saying how many seconds until one of the earlier requests is a minute old:

```json
{"detail": "Too many requests. Try again in 42 seconds."}
```

The window slides with each request instead of resetting on the minute, so sending 60 requests just before a minute ends and 60 more just after doesn't work. Requests that get a 429 don't count toward the limit. Set `RATE_LIMIT_PER_MINUTE` to change the limit. If Redis is down, requests aren't limited.

### WhatsApp webhook

Twilio calls `POST /webhooks/twilio` for each WhatsApp message a customer sends, with the message as a form, and reads the reply from the response as TwiML (XML).

Each message becomes part of a ticket:

- **The customer** is found by their WhatsApp number, or added, named after their WhatsApp profile (or their number, if they have no profile name). A customer an agent already added keeps their name.
- **The ticket** is their newest open or pending ticket. If they have none, a new ticket opens, with the first line of the message as its subject (cut to 80 characters).
- **A reply to a pending ticket** makes it open again. Pending means the team is waiting on the customer, so once they answer, it's the team's turn.
- **Messages sent at once** by the same customer land on one ticket. Each request locks the customer's row (`SELECT ... FOR UPDATE`) while it works, so a second message waits for the first instead of opening a second ticket.
- A message with no text, such as a photo, is stored as a note saying so. Media isn't saved yet.
- **A repeat delivery changes nothing.** Twilio can send the same message twice, with the same `MessageSid`, such as when it gave up waiting for the first response. Each stored message keeps its `MessageSid` in a unique column, so a repeat isn't stored again, doesn't reopen a ticket or open a new one, and gets the same reply as the first delivery. Twilio only repeats a delivery when it didn't use the first reply.
- **A message that opens a ticket gets a reply:** "Thanks, we got your message. Your ticket number is 42, and someone from our team will reply here soon." The reply goes back in the response as TwiML, so Twilio sends it without another API call. Later messages on the same ticket get no reply, so a customer who sends five short messages isn't sent five confirmations.

- **Only Twilio can call it.** Twilio signs each request with the account's auth token, in the `X-Twilio-Signature` header. A request with a missing or wrong signature gets a `403`. Until `TWILIO_AUTH_TOKEN` is set in `.env`, every request gets a `503`.
- **No IP rate limit.** Every customer's message arrives from Twilio's servers, so a limit per IP would hold all customers back together.
- **Behind ngrok,** the signature covers the public `https://` URL Twilio called. uvicorn rebuilds that URL from the `Host` header and from ngrok's `X-Forwarded-Proto` header, which it trusts because ngrok connects from 127.0.0.1.

### Urgent ticket alerts

When an agent changes a ticket's priority to `urgent`, the API posts the ticket to an n8n webhook (`N8N_WEBHOOK_URL`), and the n8n workflow in `infra/n8n/` posts it to Slack:

```json
{"ticket_id": 42, "subject": "Card charged twice", "customer_name": "Jane Doe", "status": "open",
 "priority": "urgent", "created_at": "2026-10-19T14:02:11.520331Z", "marked_urgent_by": "Ada Lovelace"}
```

- Only the API can trigger the workflow. Each call carries a shared secret in the `X-Webhook-Secret` header (`N8N_WEBHOOK_SECRET`), and n8n's Header Auth rejects any call without it. No alerts are sent until the secret is set.
- Only a change to urgent sends an alert. Sending `urgent` again for a ticket that already is sends nothing.
- The call runs after the response is sent (a FastAPI background task), so the agent never waits on n8n or Slack.
- If n8n is down or answers with an error, the update still succeeds, and a warning is logged. Nothing retries yet. Stage 6's job queue can.
- The customer's phone number is left out. The channel doesn't need it.

To run n8n, connect Slack, and import the workflow, follow [infra/n8n/README.md](infra/n8n/README.md).

### Status codes

| Code | When |
| --- | --- |
| 200 OK | A list, a ticket, or an updated ticket |
| 201 Created | A new ticket was opened |
| 401 Unauthorized | The login failed, or the request's token is missing, expired, or invalid |
| 403 Forbidden | The agent is logged in but isn't allowed to do this, such as a non-admin managing agents |
| 404 Not Found | No ticket or agent has that id |
| 409 Conflict | An agent with that email already exists |
| 422 Unprocessable Content | The input breaks a rule: a missing, unknown, or invalid field, or an id, `skip`, or `limit` out of range |
| 429 Too Many Requests | This IP made more than 60 requests in the last 60 seconds |

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

- `app/`: the API. `main.py` creates the app, `routers/` has the endpoints (one file per area), `models.py` the database tables, `schemas.py` the request and response shapes, `database.py` the database connection, `cache.py` the Redis cache, `rate_limit.py` the rate limit, and `config.py` the settings.
- `alembic/`: database migrations.
- `scripts/`: the seed script, the weekly report, and the scripts that measure the list query and the cache.
- `tests/`: pytest tests.
- `docs/`: the roadmap, stage summaries, [measured results](docs/results.md), and [system design notes](docs/system-design/).
