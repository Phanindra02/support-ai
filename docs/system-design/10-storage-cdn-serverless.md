# Object storage, CDNs, and serverless functions

When each is the right tool, as it applies to this project.

## Object storage (S3)

Object storage keeps files ("objects") by key, over HTTP, with no file system and no server to run. It's cheap ($0.023 per GB-month), very durable (S3 keeps copies across zones), and scales to any size, but you replace whole objects rather than edit them, and every read is a network call.

- **Use it for** files that are written once and read many times: uploads, backups, logs, build outputs, model files.
- **Not for** data you query or update in place, which belongs in a database.
- **Here:** agents' PDFs (`uploads/`) and their text (`text/`) are in S3, and the database rows only say where. The API never writes uploads to the server's disk, which keeps it stateless (`09-scaling-and-availability.md`). Stage 7 stores model files there too.

## CDNs (CloudFront)

A CDN copies content to servers near users ("edge locations") and answers from there, so far-away users get it faster and the origin gets fewer requests. It suits static files (images, scripts, videos) and responses that are the same for everyone. It doesn't help responses that differ per user, like an agent's ticket list.

- **Here:** there's no CDN. The API's responses are per user, the help docs are private, and the users are a support team and WhatsApp customers, whose messages go through Twilio anyway. A public website or a help center with images would be the time to add CloudFront in front of S3.
- Cloudflare, which holds the domain's DNS, could act as one if the record were proxied (orange cloud). It's DNS only, so Caddy sees each client directly (`infra/aws/README.md`).

## Serverless functions (Lambda)

A function runs your code when an event arrives (an upload, a queue message, an HTTP request) and is billed by the millisecond, with nothing to keep running between events. AWS runs, scales, and patches the machines.

- **Good for** short, occasional, event-driven work: reacting to uploads, cron jobs, webhooks, glue between services.
- **Less good for** steady high traffic (an always-on server is cheaper), work over 15 minutes, or anything that needs a warm in-memory state, since a new container starts cold.
- **Here:** pulling text out of a PDF happens a few times a day, takes about a second, and shouldn't slow down the API. So it's a Lambda (`lambda/extract_text.py`), started by S3 when a PDF arrives, at essentially no cost. The first run's numbers, including the 655 ms cold start, are in `docs/results.md`.

## In an interview

"Why not do the extraction in the API?" A big PDF would hold an API worker for seconds, and a crash in pypdf would happen inside the API. In a Lambda it's isolated, retried by AWS, and scales on its own. "Why not a queue and a worker?" Stage 6 adds one for embeddings, which is longer, heavier work. For one quick step per upload, S3's own trigger is simpler.
