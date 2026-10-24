# Stage 6: RAG with LangChain and pgvector

## What was built

Customers' WhatsApp questions are now answered from the uploaded help docs, with the docs named as sources. Questions the docs don't answer go to a person as a ticket.

1. **Ingest.** An agent uploads a PDF. The Lambda (Stage 5) pulls out its text and calls the API, which queues a job in Redis (RQ). The worker reads the text from S3, splits it into chunks of about 1,000 characters with 200 of overlap (LangChain's `RecursiveCharacterTextSplitter`), embeds each with OpenAI's `text-embedding-3-small`, and saves them to a `chunks` table with a pgvector `vector(1536)` column.
2. **Answer.** `app/bot.py` embeds the question, has pgvector find the 4 closest chunks by cosine distance, and asks `gpt-4.1-mini` to answer only from them, or to say exactly "I don't know." The model ends with the excerpts it used, which become the sources.
3. **WhatsApp.** The webhook asks the bot first. If it answers, the customer gets the answer and its sources. If not, or if OpenAI fails, the message has no text, or the customer already has a ticket open, a ticket opens and the customer is told someone will follow up.
4. **Cache.** Answers are cached in Redis for a day, keyed by the cleaned-up question and a docs version that changes whenever a document's chunks do.
5. **Measured.** On 25 questions about three sample help docs: retrieval 24/24, answers 25/25, sources 25/25 (`docs/results.md`).
6. **Reliable jobs.** A failed worker job is retried 3 times with growing waits, then kept in RQ's failed list, which `scripts/failed_jobs.py` lists and requeues.

`POST /ask` lets logged-in agents try the bot.

## Key decisions, and why

- **pgvector in the existing Postgres, not a separate vector database.** One database to run, back up, and pay for. Chunks sit next to the documents they belong to, with a foreign key and transactions. At this size, an exact search over every chunk takes milliseconds. An HNSW index is the next step if that changes.
- **One provider, OpenAI, for both embeddings and answers.** One key to guard and one bill. Questions and chunks must use the same embedding model, or their vectors can't be compared.
- **Embedding in a background job.** It calls S3 and OpenAI and can take seconds or fail, so the Lambda's webhook returns at once. The job replaces a document's chunks in one transaction, so running it twice is safe. That's what makes at-least-once delivery and retries safe too.
- **"I don't know" instead of guessing.** The prompt says to answer only from the excerpts, and the bot checks for the exact phrase. A wrong refund policy is worse than handing the question to a person.
- **No retries for the bot, 6 seconds per call.** Twilio waits 15 seconds. Two calls of 6 seconds still leave time to open a ticket, and a person is a better fallback than a late answer.
- **Docs versioning for the cache.** It invalidates every cached answer at once when the docs change, without finding each key. Redis drops the old ones when they expire.
- **`/ask` needs a login.** Every question costs money, so it isn't public. Customers reach the bot through Twilio, whose signature the webhook checks.
- **Measuring in memory.** `scripts/measure_bot.py` embeds the docs and ranks chunks with the same cosine distance pgvector uses, without a database, so trying another chunk size is one command.

## How to run and demo it

```bash
docker compose up -d                        # Postgres with pgvector, Redis, the API, the worker
uv run alembic upgrade head
uv run python -m scripts.measure_bot        # the 25 questions, about $0.02 with the key in .env
uv run python -m scripts.failed_jobs        # jobs that failed after all their tries
```

On the live server, upload the PDFs in `docs/help-docs/` through `POST /documents` at https://support.mysolospark.com/docs, then ask `POST /ask` "How much is standard shipping?". It answers $5.99 and names `shipping-and-delivery.pdf`. On WhatsApp, the same question gets the same answer, and "Do you have a store in Chicago?" opens a ticket.

## What an interviewer might ask

- **What is RAG, and why not fine-tune?** Retrieval-augmented generation finds the relevant text and puts it in the prompt. Updating the docs updates the answers at once, sources can be shown, and nothing is trained. Fine-tuning teaches style, not facts you can cite.
- **Why cosine distance?** It compares the direction of two vectors and ignores their length, and OpenAI recommends it for its embeddings.
- **How do you stop the bot from making things up?** It's told to use only the excerpts and to say an exact phrase when they don't hold the answer, at temperature 0. That phrase sends the question to a person. The measurement checks it with a question the docs don't answer.
- **How do you know it works?** 25 questions with known answers. The first real run found a bug the fakes couldn't: "I don't know." followed by "Excerpts used: None" counted as an answer.
- **What happens if OpenAI is down?** `/ask` returns a 503. WhatsApp customers get a ticket instead of an answer. Worker jobs retry 3 times, then wait in the failed list.
- **How would you choose chunk size?** Measure it. Here, five settings scored the same, because three short docs are too easy a test. Telling them apart needs bigger docs and questions whose answers span sections.
- **What would you do at a million documents?** An HNSW index on the embedding column, more workers on the queue, and filtering chunks by product or language before the vector search.
- **What happened when the docs didn't show up?** The worker's job failed with `AccessDenied`, because the server's IAM role in AWS didn't have the permission the repo's policy file had. Fixing the role and requeueing the three failed jobs embedded the docs, with no new uploads.
