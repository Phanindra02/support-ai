# Stage 3: Auth, Twilio, and n8n

Done when: agents log in with JWTs, WhatsApp messages become tickets, and urgent tickets post to Slack through n8n.

**Status: done.** All three parts were checked live on October 21, 2026, with a real phone, Twilio's WhatsApp sandbox, ngrok, n8n, and Slack (see [Checked live](#checked-live)).

- **Logins with JWTs:** done and tested.
- **WhatsApp messages become tickets:** done. Real messages from the sandbox opened a ticket and got the confirmation, and later messages joined that ticket.
- **Urgent tickets post to Slack through n8n:** done. Marking a ticket urgent in `/docs` posted the alert in `#support-urgent`.
- **Tests:** 366 pass, with no network calls.

## What was built

- **Routers.** The endpoints moved out of `app/main.py` into `app/routers/` (`auth`, `agents`, `tickets`, `webhooks`), following FastAPI's "Bigger Applications" layout. This had to come first: the rate limit sat on the whole app, and the Twilio webhook needed to skip it.
- **Agent logins** (`app/security.py`, `app/routers/auth.py`):
  - `POST /auth/token` checks an email and password and returns a JWT, following FastAPI's security tutorial.
  - Passwords are hashed with Argon2id (pwdlib).
  - Tokens are HS256 and last 30 minutes.
  - `GET /agents/me` shows who is logged in.
- **Roles.** Every ticket endpoint needs a logged-in agent. Admins can also manage agents: `GET /agents`, `POST /agents`, and `PATCH /agents/{id}`. `scripts/create_agent.py --admin` makes the first admin.
- **The Twilio webhook**, `POST /webhooks/twilio`:
  - It checks Twilio's signature on every request.
  - It finds or adds the customer by their WhatsApp number.
  - It adds the message to their open ticket, or opens a new one.
  - It answers a new ticket with a confirmation in TwiML.
  - A repeat delivery of the same message changes nothing.
- **Urgent alerts** (`app/alerts.py`). When a ticket's priority changes to urgent, the API posts it to an n8n webhook, sending a shared secret in a header. This runs after the response has gone out.
- **The n8n workflow** (`infra/n8n/urgent-ticket.json`). A Webhook trigger with Header Auth, then a Slack node that posts to `#support-urgent`. `infra/n8n/README.md` has the setup steps.
- **Two migrations:** agent passwords, the active flag, and roles; then `messages.twilio_sid`, unique.
- **Docs:**
  - Notes on [authentication](../system-design/05-authentication.md): authn vs authz, sessions vs JWTs, and the OAuth2 flows.
  - Notes on [webhooks](../system-design/06-webhooks.md): retries, at-least-once delivery, and idempotency.
  - New README sections for logins, roles, the webhook, and alerts.

## Key decisions and why

**The OAuth2 password flow, not an identity provider.** The API is both the login server and the API being called, and only its own agents log in, so there's no third party to delegate to. FastAPI's tutorial uses this flow, and `/docs` understands it. If outside apps ever needed access, the right move is authorization code with PKCE, probably from Auth0, Cognito, or Okta.

**Look up the agent on every request, even with a JWT.** A JWT can't be taken back once it's issued. Loading the agent by id on each request, and checking `is_active` and `role`, costs one indexed lookup. In return, deactivating or demoting an agent takes effect at once instead of when their token expires. The token carries only `sub` (the id, which never changes) and `exp`, and both are required.

**Don't reveal which emails have accounts.** A wrong email and a wrong password get the same message. When no agent has that email, the password is still checked against a dummy hash, so both failures take the same time (about 30 ms).

**Pin the JWT algorithm.** Decoding accepts `HS256` only. Without that list, a forged token could pick its own algorithm, such as `none`. HS256 rather than RS256, because only this API makes and checks tokens.

**Roles in one dependency, checked by a test.** `CurrentAgent` gives a 401 and `CurrentAdmin` gives a 403. A test reads the OpenAPI schema and fails if any endpoint other than `/` and `/auth/token` lacks the login check, so a new endpoint can't forget it. An admin can't demote or deactivate themselves, so one admin always remains.

**`POST /tickets` needs a login too.** Its response shows the name stored for a phone number, so leaving it open would let anyone look up customers by number. Customers open tickets through WhatsApp.

**Twilio's signature is the webhook's login.** Twilio signs the full URL plus every form field with HMAC-SHA1, keyed with the auth token. The twilio library's `RequestValidator` recomputes it.
- **Behind ngrok,** the URL must be the public `https://` one. uvicorn rebuilds it from the `Host` header and from `X-Forwarded-Proto`, which it trusts from 127.0.0.1, where ngrok connects from.
- **A file upload** can't be signed and would crash the check, so it gets a 403, not a 500.
- **Until `TWILIO_AUTH_TOKEN` is set,** the webhook answers 503 to everything.

**The webhook skips the per-IP rate limit.** Every customer's message arrives from Twilio's servers, so one limit per IP would hold all customers back together. The signature check means only Twilio can call it anyway.

**Messages sent at once share a ticket.** WhatsApp users send bursts of short messages, and Twilio delivers them together. Each request locks the customer's row (`SELECT ... FOR UPDATE`) before looking for an open ticket. The second request waits, then finds the ticket the first one opened. A test holds the lock from another session to prove it.

**Repeat deliveries are stored once.** Twilio delivers at least once, so the same message can arrive twice with the same `MessageSid`.
- **What the webhook does.** It stores the id in a unique column, and looks the id up after taking the customer's lock. A repeat that arrives mid-save waits, then finds the stored message.
- **Why the order matters.** If the lookup ran before the lock, both requests would miss it, and the unique constraint would turn the second into a 500. A test proves this: it fails if the lookup moves.
- **A repeat gets the same reply as the first time.** Twilio only repeats a delivery when it didn't use the first reply, so the customer never saw that confirmation.

**Replies go back as TwiML, not through Twilio's REST API.** The reply rides on the webhook's own response, so there's no second request to fail and no account SID to store. Only a message that opens a ticket gets one, so five quick messages don't get five confirmations.

**Take what Twilio sends, without refusing it.** The form model doesn't forbid extra fields, because Twilio sends about 20 and adds more over time. NUL characters, which Postgres can't store, are removed rather than refused. A 422 would make Twilio drop the customer's message.

**n8n owns the Slack part.** The API posts JSON to a webhook, and the workflow decides the channel and the wording. Changing either needs no code change or deploy.

**Alerts can't break the API.** The call runs as a FastAPI background task after the response, with a 5-second timeout. If n8n is down or refuses, the ticket is still saved and a warning is logged. Nothing retries yet, so an alert can be lost. Stage 6's queue can fix that, and the [webhook notes](../system-design/06-webhooks.md) explain what a safe retry needs.

**Only a change to urgent alerts.** Marking an urgent ticket urgent again sends nothing, so repeating the same PATCH doesn't post twice.

**A shared secret, checked by n8n.** Anyone who could reach n8n's URL could otherwise post fake urgent tickets. The API sends `N8N_WEBHOOK_SECRET` in `X-Webhook-Secret`, and n8n's Header Auth rejects anything else with a 403. Until the secret is set, no alert is sent.

**Customer text is escaped in Slack.** The subject and name come from customers. Slack reads `<...>` as a link or mention, so a WhatsApp message starting with `<!channel>` would notify the whole channel. The workflow escapes `&`, `<`, and `>`, and `tests/test_n8n_workflow.py` checks it.

**The customer's phone number stays out of Slack.** The channel doesn't need it, and fewer copies of personal data means fewer places for it to leak from.

**Secrets never come from the repo.** `.env.example` leaves `JWT_SECRET_KEY` empty, because a placeholder long enough to pass the length check would let anyone who copied it sign tokens with a public key. The app refuses to start until a real one is set, and a test checks that. The n8n export only names its credentials, and n8n keeps their values in its own encrypted store.

## How to run and demo it

```bash
uv sync
uv run alembic upgrade head
uv run python -m scripts.create_agent --email you@example.com --name "Your Name" --admin
uv run pytest                                  # 366 tests, about 13 seconds
uv run uvicorn app.main:app --reload           # http://127.0.0.1:8000/docs
```

Without any accounts:

1. **Login.** In `/docs`, call `GET /tickets` and get a 401. Click Authorize, log in, and call it again.
2. **Roles.** Make a plain agent with `POST /agents`, log in as them, and try `GET /agents`: 403.
3. **Lockout.** As the admin, set that agent's `is_active` to false. Their token, still unexpired, now gets a 401.
4. **The webhook's tests.** Run `uv run pytest tests/test_whatsapp.py -v`. The tests play Twilio: they sign forms with a made-up token. Point out the lock test and the repeat-delivery tests.

With the accounts (the four setup steps are in [infra/n8n/README.md](../../infra/n8n/README.md) and the roadmap):

5. **WhatsApp to ticket.** Send the sandbox number "Where is my order?" and get the confirmation with a ticket number. Send "It's order 1234", and it joins the same ticket with no second reply. Show both in `GET /tickets/{id}/messages`. Then close the ticket with a PATCH and message again: a new ticket opens, with its own confirmation.
6. **Signature.** `curl -X POST https://<ngrok-host>/webhooks/twilio -d Body=hi` gets a 403.
7. **Slack.** In `/docs`, PATCH that ticket to `{"priority": "urgent"}`, and the alert appears in `#support-urgent`.
8. **n8n is down.** Stop n8n, mark another ticket urgent: the update still succeeds, and the API logs a warning.

## What an interviewer might ask

- **401 or 403?** 401 means "I don't know who you are". 403 means "I know, and no".
- **Why JWTs, and how do you log someone out?** You can't take a JWT back. Short expiry, and a database lookup per request so deactivation is immediate. A refresh token, or a list of revoked tokens, are the other options.
- **Why look the agent up if the token is signed?** To make deactivation and role changes take effect at once. It costs one primary-key lookup.
- **Why Argon2 and not SHA-256?** SHA-256 is built to be fast, so a stolen table can be guessed at billions per second. Argon2id is slow and memory-hungry on purpose, and salted per password.
- **How do you know a request came from Twilio?** HMAC signature over the URL and the form. Why the URL is part of it, and what goes wrong behind a proxy.
- **Two messages arrive at once from a new customer. What happens?** The unique constraint on phone plus a savepoint handles the customer, and the row lock handles the ticket.
- **Twilio sends the same message twice. What happens?** Unique `twilio_sid`, looked up after the lock, and the same reply returned. Why the order matters.
- **Why not call Twilio's API to reply?** TwiML in the response is one request instead of two, with nothing extra to fail.
- **Why does the alert run in a background task, and what if n8n is down?** The agent doesn't wait, and failures are logged without retries, so an alert can be lost. How Stage 6's queue would fix it, and what makes a retry safe.
- **Why put n8n in the middle instead of calling Slack directly?** Non-developers can change the message and the channel, and add steps like paging someone, without a deploy. The cost is one more service to run and secure.
- **What stops someone posting fake alerts to n8n?** The shared secret header. What a signature with a timestamp would add.
- **A customer's follow-up gets a 200 but no reply. Is that a bug?** No. An empty `<Response/>` tells Twilio there's nothing to send, so five quick messages don't get five confirmations. The real gap is that agents can't reply yet (see below).

## Known limitations, for later stages

- **Agents can't reply to customers yet, so follow-ups go unanswered.** The confirmation promises a reply on WhatsApp, but no endpoint takes an agent's message, and nothing sends one through Twilio. Stage 6's bot will answer questions from the docs, but no roadmap step adds agent replies yet. One would need Twilio's REST API, and WhatsApp allows a free-form message only within 24 hours of the customer's last one. After that, it must use a template WhatsApp has approved.
- **Login shares the general limit of 60 requests a minute per IP, and that limit fails open.** A stricter limit on `/auth/token`, by IP and by email, that fails closed would slow password guessing.
- **No refresh tokens or logout.** Agents log in again every 30 minutes.
- **Alerts aren't retried.** Stage 6's RQ queue can retry with backoff, plus an id the workflow checks so a retry doesn't post twice.
- **Media isn't saved.** A photo becomes a note saying the customer sent something else.
- **The webhook works while Twilio waits.** That's fine while it only writes to Postgres. Once Stage 6 calls an LLM, it should answer Twilio at once and do the work in a job, checking `MessageSid` again there.
- **WhatsApp only works while the Mac runs the API and ngrok.** If the ngrok URL ever changes, the sandbox's webhook must be updated to match. Stage 5's domain and EC2 fix both.
- **n8n runs on the Mac with its own SQLite database.** Stage 4 moves it into docker compose.

## Checked live

On October 21, 2026, with the developer's accounts:

- **Setup.**
  - The Twilio auth token is in `.env`. The account SID isn't, because nothing uses it until the API sends messages itself.
  - ngrok has the developer's authtoken and forwards to port 8000.
  - The sandbox's "When a message comes in" points at `/webhooks/twilio` on the ngrok URL.
  - In n8n, the workflow uses the "Support AI webhook secret" Header Auth credential and the "Slack bot" credential, and it's published.
- **WhatsApp to ticket (demo step 5).** The first message opened ticket 11301005, and the confirmation came back on WhatsApp with that number. Ten later messages joined the same ticket with no reply, as designed, and each was stored once with its own `MessageSid`. After the ticket was closed, the next message opened ticket 11301008.
- **Signature (demo step 6).** An unsigned POST through the ngrok URL got a 403 with `{"detail":"Invalid Twilio signature"}`.
- **Slack (demo step 7).** PATCHing a ticket to `{"priority": "urgent"}` in `/docs` posted the alert in `#support-urgent`.
- **n8n down (demo step 8)** wasn't run live. `test_the_update_succeeds_even_if_the_alert_fails` in `tests/test_alerts.py` covers it.

## Changes made on the developer's Mac

- **ngrok 3.39 (Homebrew)** is installed, with the developer's authtoken.
- **`.env`** has a generated `JWT_SECRET_KEY` and `N8N_WEBHOOK_SECRET`, and the developer's `TWILIO_AUTH_TOKEN`.
- **The local `support` database** is migrated to the latest revision, `54c6c062563f`. It also holds today's demo tickets, 11301005 to 11301008.
- **n8n 2.35.7 runs with `npx n8n`**, and keeps the workflow and both credentials in `~/.n8n`. The root-owned files in `~/.npm` that made `npx n8n` fail have been fixed.
- **An older n8n, 1.64.3, is also installed globally** at `/opt/homebrew/bin/n8n`, from an earlier `npm install -g`. Typing plain `n8n` starts that one, and it may not work with the `~/.n8n` database that 2.35.7 has upgraded. Use `npx n8n`, or remove the old one with `npm uninstall -g n8n`.
