# Message queues and background jobs

## Why the project has one

Embedding a help doc means reading its text from S3, splitting it, and waiting on OpenAI. That can take seconds, and OpenAI can be down. The Lambda's webhook shouldn't wait on any of it, so the API puts a job on a queue in Redis (RQ) and returns at once. The worker (`worker/main.py`) takes jobs off the queue and runs them. The API stays fast, and a slow or failed job affects no request.

## Delivery guarantees

- **At-most-once:** a job runs once or not at all. A crash mid-job loses it.
- **At-least-once:** a job runs until it succeeds, so it may run more than once. This is what most queues, RQ included, give you once retries are on.
- **Exactly-once:** very hard to get end to end. In practice you get at-least-once delivery plus *idempotent* jobs, so a second run changes nothing.

This project uses that last pattern. The embedding job is idempotent: it deletes the document's chunks and saves the new ones in one transaction, and a unique `(document_id, position)` constraint backs that up. The API also queues the job on every call of the webhook, a repeat too. So if Redis was down, the Lambda's retry queues it, and if the job runs twice, the document still has one copy of each chunk.

## Retries

A job can fail for a passing reason, like an OpenAI timeout or rate limit, or a lasting one, like a missing document. Retrying helps with the first, and a limit stops the second from looping forever.

`app/queue.py` gives the embedding job 3 more tries, after 30 seconds, 2 minutes, then 10 minutes. The waits grow (backoff), so an outage has time to pass, and many failing jobs don't all hit OpenAI again at the same moment. Retries with a wait need RQ's scheduler, so the worker starts with `with_scheduler=True`.

The bot is the opposite: a customer is waiting, so it doesn't retry at all (`app/bot.py`). Its fallback, a ticket for a person, is better than a late answer.

## Dead-letter queues

A dead-letter queue (DLQ) is where messages go after their last failed try, so they aren't lost or retried forever, and a person can look at them. SQS and RabbitMQ have them built in. In RQ, it's the failed job registry: a separate list in Redis that keeps each job with its error.

`scripts/failed_jobs.py` lists those jobs and their errors, and `--requeue` runs them again once the cause is fixed. Stage 6 needed exactly this. The first three help docs failed with `AccessDenied`, because the server's IAM role couldn't read the extracted text yet. After the role was fixed, requeueing the three failed jobs embedded the docs, with no new uploads.

## What would change at scale

- Many workers can share one queue. Each job goes to one worker.
- Redis keeps the queue in memory, with append-only saving to disk here. SQS would give a managed queue with a built-in DLQ, at the cost of tying the worker to AWS.
- An alert on the size of the failed list would catch problems sooner than someone checking.
