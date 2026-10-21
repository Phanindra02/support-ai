# Webhooks, retries, and idempotency

A webhook turns polling around. Instead of this API asking Twilio every few seconds whether a customer wrote, Twilio calls the API the moment a message arrives. This project receives one webhook (Twilio's, for each WhatsApp message) and sends one (to n8n, for each urgent ticket).

## How webhooks work

1. The receiver gives the sender a public HTTPS URL, such as `https://<ngrok-host>/webhooks/twilio`.
2. When something happens, the sender POSTs the details there, as a form or JSON.
3. The receiver answers with a 2xx status quickly. Anything else, or no answer in time, counts as a failure.

Because the URL is public, anyone can call it, so the receiver has to check who sent each request. The usual ways, strongest first:

- **An HMAC signature.** The sender signs the request with a secret both sides know, and the receiver recomputes it. Twilio signs the full URL plus every form field with HMAC-SHA1 and the account's auth token. Stripe and GitHub sign the body with HMAC-SHA256. A good signature covers a timestamp too, so an old request can't be sent again later (a replay attack).
- **A shared secret in a header.** Simpler, but it proves nothing about the body, and anyone who sees one request can make their own. HTTPS keeps it from being read on the way.
- **Mutual TLS, or allowing only the sender's IP addresses.** These need more setup, and IP lists change.

## Why deliveries fail, and why they repeat

The network between sender and receiver can fail at any point:

- **Before the request arrives.** The receiver is down, or the connection times out. The receiver never saw it.
- **After the receiver did the work, before the answer arrives.** The receiver saved the message, then the connection dropped, or the answer took too long. The sender can't tell this from the case above.
- **The receiver answered with an error.** A 500 usually means "try again later". A 400 usually means "this will never work".

From the sender's side, a lost answer looks exactly like a lost request. To avoid losing events, senders retry, and that means the receiver can get the same event twice. This is **at-least-once delivery**, and nearly every webhook sender works this way.

**Exactly-once delivery** across a network isn't possible in general, because the sender can never be sure the receiver acted. What systems offer instead is **exactly-once processing**: at-least-once delivery, plus a receiver that recognizes repeats. That second half is the receiver's job.

### How senders retry

- **Timeouts.** A sender waits a fixed time for an answer. Twilio waits 15 seconds by default. A slow receiver gets retried even though it finished the work.
- **Backoff.** Retries wait longer each time (1 s, 2 s, 4 s, ...) and add some randomness (jitter), so a receiver coming back from an outage isn't hit by every retry at once.
- **A limit.** After some number of attempts or hours, the sender gives up. Stripe retries for up to three days. Many senders then show failed events in a dashboard, so they can be resent by hand.
- **Order isn't guaranteed.** A retried event can arrive after a newer one.

**Twilio's defaults.** For an incoming message, Twilio retries once, and only if it couldn't connect at all. Connection overrides added to the webhook URL can change this, for example `#rc=3&rp=rt,5xx` for three retries on read timeouts and server errors. Each attempt carries an `I-Twilio-Idempotency-Token` header, and every attempt for one message has the same `MessageSid`.

## Idempotency

An operation is **idempotent** if doing it twice has the same effect as doing it once. `PUT /tickets/42 {"status": "closed"}` is idempotent: the second call finds the ticket already closed. "Add this message to the ticket" isn't: the second call adds it again.

A webhook handler has to be idempotent, because it will get repeats. The usual tools:

- **An idempotency key.** Each event carries a unique id: Twilio's `MessageSid`, Stripe's event id, GitHub's `X-GitHub-Delivery`. The receiver stores the ids it has processed, and skips any it has seen.
- **A unique constraint on that key.** Checking "have I seen this id?" in code isn't enough by itself. Two copies of the same event can arrive at the same moment, both check, both see nothing, and both save. The database's unique constraint is the only check that can't race. The code check gives a clean answer, and the constraint is the guarantee.
- **The same answer for a repeat.** Idempotent means the repeat gets the result the first call got, not just that nothing changes. Stripe's API stores each response under its idempotency key and returns it again for a repeat.
- **Effects that are naturally idempotent.** "Set the status to closed" can run twice safely. "Add 1 to the count" can't.
- **Answer fast, work later.** Save the event, answer 2xx, and do slow work in a background job. A slow handler gets retried while it's still working, which makes repeats more likely, not less.

Outside effects need the same care. If the handler sends an email or calls a payment API, a repeat must not send it again. Either record that it was sent, in the same transaction as the event, or pass an idempotency key to the API being called.

## What this project does

### Receiving: the Twilio webhook skips messages it already stored

Each stored WhatsApp message keeps Twilio's `MessageSid` in `messages.twilio_sid`, which has a unique constraint. The code is in `app/routers/webhooks.py`.

1. **Check the signature.** A request Twilio didn't sign gets a `403`, before anything is read or stored.
2. **Lock the customer's row** (`SELECT ... FOR UPDATE`). Stage 3 already did this, so two messages sent at once don't open two tickets.
3. **Then look for the `MessageSid`.** If it's stored, nothing changes: no new message, no reopened ticket, no new ticket.
4. **Otherwise,** add the message with its `MessageSid`, and commit.

The order matters. Every delivery of one message comes from one customer, so it needs that customer's lock. A repeat that arrives while the first delivery is still saving waits for the lock, then finds the stored message. Postgres's default isolation level (read committed) lets each new statement see what other transactions have committed, so the lookup after the lock sees the first delivery's row. If the lookup ran before the lock, both deliveries would see nothing, and the second would hit the unique constraint and fail with a 500. `test_a_repeat_that_arrives_while_the_first_is_saving_waits_for_it` holds the lock to prove this, and it fails if the lookup is moved before the lock.

**A repeat gets the same reply as the first delivery.** If the message opened its ticket, the reply is the confirmation with the ticket number. Otherwise it's an empty `<Response/>`. The reason to send the confirmation again: Twilio only repeats a delivery when it didn't get, or didn't use, the first answer, so the customer never saw that confirmation. To know whether a stored message opened its ticket, the handler checks whether it's the ticket's first message. That works because every way a ticket opens (`POST /tickets` and this webhook) saves its first message with it.

**Why `MessageSid` and not `I-Twilio-Idempotency-Token`.** `MessageSid` is part of the signed form, it's the same however the message reaches the API, and it's worth keeping anyway, to match a stored message with Twilio's logs. The token is only a header.

**Why not compare the text.** A customer can send "Hello?" twice on purpose. Only the sender knows whether two deliveries are the same event, so only its id can say.

**What isn't handled yet.** The confirmation goes back in the response itself, as TwiML, so it isn't a separate outside call that a repeat could make twice. When Stage 6 has the bot answer, and calls an LLM before answering, the handler will get slower. It should then answer Twilio at once and do the work in an RQ job, which needs the same `MessageSid` check, so a retried job doesn't answer twice.

### Sending: the urgent-ticket alert to n8n

The API is the sender here, and it does the simple thing: one try, a 5-second timeout, and a logged warning if n8n is down or refuses. It doesn't retry yet, so n8n can't get a repeat, and an alert can be lost.

Retrying would bring the problems above to n8n. If n8n posted to Slack and the answer was lost, a retry would post the same alert twice. A safe version would put the alert on a queue (Stage 6's RQ), retry it with backoff, and include an id such as `ticket-42-urgent-1` that the workflow checks before posting. For an alert a human reads, an occasional lost or doubled message is a small cost. The ticket is still marked urgent, and an agent's dashboard still shows it.

The alert is checked with a shared secret in a header, not a signature, so it proves the caller knows the secret but not that the body is unchanged. Both ends run on the same machine, or on a private network from Stage 5, which makes that acceptable.

## Interview questions

- Why can't a webhook sender promise exactly-once delivery?
- What makes a handler idempotent? Give an operation that is, and one that isn't.
- Two copies of the same event arrive at the same moment. How do you make sure only one is processed?
- Why answer a webhook quickly and do the work in a background job?
- A handler sends an email. How do you stop a retry from sending it twice?
- How does a receiver know a webhook really came from the sender? What does a timestamp in the signature add?
- Why do retries use exponential backoff with jitter?
- A retried event arrives after a newer one. When does that matter, and what can you do about it?
