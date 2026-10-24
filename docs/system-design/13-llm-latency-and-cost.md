# Controlling an LLM app's latency and cost

Each bot answer is two OpenAI calls: one embeds the question (`text-embedding-3-small`), and one writes the answer (`gpt-4.1-mini`). Both cost money and take time, so the project limits each.

## Caching

The cheapest call is the one you don't make. `app/bot.py` caches each answer in Redis for a day, keyed by the cleaned-up question (lowercased, single-spaced, without `?`, `!` or `.` at the end) and hashed to a fixed length. A cache hit makes no OpenAI calls, so it costs nothing and needs only a Redis lookup. Its latency hasn't been measured yet.

The hard part is staleness. A cached "I don't know." is wrong once a doc that answers it is uploaded. Rather than finding and deleting old answers, every key holds a docs version, which the worker bumps after it saves a document's chunks. Old answers are then never read again, and Redis drops them when they expire.

A semantic cache, which also matches questions with the same meaning, would hit more often. But it needs an embedding call per question, and it can return a wrong answer for a question that is close in meaning but different. The exact-match cache can't.

## Batching embeddings

The worker embeds all of a document's chunks in one request (LangChain sends up to 1,000 per call), not one request per chunk. That means fewer round trips, less time, and fewer requests counted against OpenAI's rate limits. The price per token is the same either way. For large offline jobs, OpenAI's Batch API halves the price in exchange for answers within 24 hours.

## Limiting context size

The model charges for every token in and out, and longer prompts are slower. So:

- Only the 4 closest chunks of about 1,000 characters go in the prompt, roughly 1,000 tokens, not whole documents.
- Answers are capped at 300 tokens, which also keeps them within one WhatsApp message.
- Questions are capped at 1,600 characters, the most WhatsApp sends.
- With no chunks at all, the LLM isn't called.

From OpenAI's prices, an uncached question costs an estimated $0.0007. The measurement in `docs/results.md` showed 2 chunks scored as well as 4 on this test set, and the setting stays at 4 for answers that span two places.

## Latency limits

Twilio waits 15 seconds for the webhook's reply. Each OpenAI call gets 6 seconds and no retries, so even two slow calls leave time to fall back to a ticket. Picking a small model and temperature 0 helps too. Streaming the answer would make it feel faster in a chat UI, but WhatsApp gets the whole message at once, so it wouldn't help here.
