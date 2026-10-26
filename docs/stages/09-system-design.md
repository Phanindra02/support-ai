# Stage 9: System design practice

**Status:** the written work is done. The mock interviews are still yours to do, and the stage's "Done when" (designing a common system out loud in 45 minutes) is met only once you can do it yourself.

## What was built

- **The platform's design doc** (`docs/design-doc.md`): requirements, scale estimates, architecture and data model diagrams, bottlenecks, and trade-offs. A test keeps it in step with the code: every table must appear in its data model.
- **The interview flow and quick estimates** (`docs/system-design/practice/00-interview-flow-and-estimates.md`): a 45-minute time budget, the numbers worth knowing by heart, and how to do the arithmetic.
- **Six worked designs** (`01` to `06`): a URL shortener, a rate limiter, a notification system, a chat app, a news feed, and Dropbox-like file storage. Each follows the same seven steps, has a diagram, and ties back to this project where it already does the same thing.
- **The platform at 10 million users** (`07`), with a table comparing each part with what's built.
- **Three mock interview scripts** (`08`): a rate limiter, this project in depth, and a chat app, with follow-up questions, a rubric, and a log to fill in.
- **A test** (`tests/test_system_design_practice.py`) that checks each design keeps the flow's headings in order, has a diagram, and links only to files that exist.

The practice write-ups were written by Claude Code at your request, which overrode the CLAUDE.md rule against doing this practice for you.

## Key decisions, and why

- **One fixed flow for every design.** Interviewers grade the process as much as the answer. With the same seven headings every time, the order becomes automatic, and the test keeps every write-up to it.
- **Each design links back to this project.** "I built a sliding-window rate limiter that fails open after 0.5 seconds" is stronger in an interview than a textbook answer. The rate limiter, the answer cache, `twilio_sid` idempotency, the `OFFSET` measurement, and the lost-alert gap all come up.
- **No sharding at 10 million users.** The arithmetic shows about 7 writes a second, which one Postgres handles. Saying so, with the numbers, is better than reaching for sharding because the number of users sounds large. The real changes are answering from a queue, tenancy, vector search, durability, and OpenAI cost.
- **Shared tables with row-level security for tenancy,** over a database or schema per company: one migration and one schema to run, with Postgres refusing other companies' rows even if a query forgets its filter.

## How to use it

1. Read `00` once, and learn the "numbers to know" table.
2. For each of `01` to `06`: set a 45-minute timer, design it out loud on a blank page, *then* read the write-up and note what you missed.
3. Read `07` alongside the design doc, until you can explain each row of its comparison table.
4. Do the three mocks in `08`, and fill in the log.

## What an interviewer might ask

- **"How would this scale to 10 million users?"** Do the arithmetic first: 500,000 messages a day is about 17 a second at busy hours, and the database sees about 7 writes a second. So the database isn't the problem. The webhook waiting on OpenAI is, then tenancy, vector search, and the OpenAI bill. Walk through `07`'s table.
- **"How do you keep one company's data from another's?"** A `company_id` on every table, first in every index, and Postgres row-level security set per transaction, so a forgotten filter returns nothing.
- **"Which rate limiting algorithm would you use?"** It depends on memory. This project uses a sliding-window log, which is exact and fine at 60 a minute per IP. For millions of clients, a token bucket or sliding-window counter needs only a few bytes per client.
- **"Fan-out on write or on read?"** Both: push for ordinary users, pull for accounts with millions of followers, merged at read time.
- **"How do you make sure a notification is sent once?"** An idempotency key with a unique constraint stops duplicates from callers. A status check before sending stops queue redeliveries. An outbox stops losses between the database and the queue. Exactly once can't be promised end to end, because a provider can time out after it sent the message.
