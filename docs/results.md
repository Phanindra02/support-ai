# Results

Every number measured in this project, and how it was measured, so each one can be checked and repeated.

## Ticket list query on 100,000 tickets (Stage 2)

**Data.** 100,000 seeded tickets from 49,890 customers, with 232,455 messages (`uv run python -m scripts.seed --reset --count 100000 --seed 42`). 92.8% are closed, 4.7% open, and 2.5% pending. 7% are urgent, and 235 are both open and urgent. The tickets table is 15 MB.

**Machine.** An Apple M4 Pro with 24 GB of memory, running PostgreSQL 16.15 from Homebrew with default settings: `work_mem` 4 MB, `shared_buffers` 128 MB, and up to 2 parallel workers per query.

**Method.** `uv run python -m scripts.explain_list` runs `EXPLAIN (ANALYZE, BUFFERS)` on the exact query `GET /tickets` builds (`ticket_list_query` in `app/routers/tickets.py`), with `limit=20`. Each request runs once to warm the cache, then 7 more times, and the table shows the median "Execution Time". That's time inside Postgres only, without the network or Python. Repeated runs differ by about 10%.

### Before indexes

The only indexes on tickets were the primary key and the two foreign keys.

| Request | Median | How Postgres read the table |
| --- | ---: | --- |
| First page, no filters | 11.07 ms | Parallel sequential scan of all 100,000 rows |
| `status=open` | 3.15 ms | Sequential scan |
| `priority=urgent` | 3.62 ms | Sequential scan |
| `status=open&priority=urgent` | 2.78 ms | Sequential scan |
| `status=closed` | 8.39 ms | Parallel sequential scan |
| `skip=50000` (a deep page) | 22.19 ms | Sequential scan |

What the plans show (`--plans` prints them):

- **Every request reads the whole table.** Nothing tells Postgres where the newest tickets are, so it reads all 100,000 rows, keeps the ones that match the filter, and sorts them to find the top 20.
- **The page with no filters is the slowest common request,** because all 100,000 rows go through the sort. With `status=open`, only 4,701 do.
- **The deep page is slowest of all.** To skip 50,000 rows, the sort must keep 50,020 of them in order. That needs more than the 4 MB `work_mem`, so the sort spills 7 MB to disk (`Sort Method: external merge`).

### After indexes

The migration `Index tickets by status and created time` adds two indexes. It built both on 100,000 rows in well under a second.

- `ix_tickets_created_at_id` on `(created_at, id)`, the list's sort order (3.1 MB).
- `ix_tickets_status` on `status` (696 kB), for the stats counts.

| Request | Before | After | Faster by |
| --- | ---: | ---: | ---: |
| First page, no filters | 11.07 ms | 0.02 ms | about 550x |
| `status=open` | 3.15 ms | 0.02 ms | about 160x |
| `priority=urgent` | 3.62 ms | 0.07 ms | about 50x |
| `status=open&priority=urgent` | 2.78 ms | 0.51 ms | about 5x |
| `status=closed` | 8.39 ms | 0.05 ms | about 170x |
| `skip=50000` (a deep page) | 22.19 ms | 7.59 ms | about 3x |

Times under 0.1 ms are close to the smallest EXPLAIN can measure, so read the first rows as "under a tenth of a millisecond" rather than exact ratios.

What changed in the plans:

- **Every request now reads `ix_tickets_created_at_id` backward,** newest first, and stops after 20 matching rows. Nothing is sorted.
- **Filters are checked row by row along the way.** A filter that matches recent tickets finds 20 almost at once: `status=open` skipped only 8 rows, because this week's tickets are mostly open. A rare combination walks further: `status=open&priority=urgent` skipped 2,826 rows to find 20, which still takes only 0.5 ms. An index on `(status, priority, created_at)` would make that instant too, but it would be one more index to update on every write, for 0.5 ms.
- **The deep page only got about 3x faster.** `OFFSET 50000` still reads 50,000 rows and throws them away. The index only saves the sort. Cursor pagination (see the [REST notes](system-design/02-rest-api-design.md)) would make every page as fast as the first.

### Why there's no index on priority

The first version of the migration also indexed `priority`, as the roadmap suggested. After every benchmark above, `pg_stat_user_indexes` showed that Postgres had used it **0 times**. Walking the created-time index finds 20 urgent tickets after a few hundred rows, because 7% of tickets are urgent, so the planner never needed it.

The status index is different. The list doesn't use it either, but count queries like the stats endpoint's do:

| Query | Time | How Postgres reads the table |
| --- | ---: | --- |
| Count tickets by status | 4.7 ms | Index-only scan on `ix_tickets_status` |
| Count open and pending tickets by priority | 2.2 ms | Bitmap scan on `ix_tickets_status` |

(Each is the median of 5 runs of `EXPLAIN ANALYZE` in psql.)

So the migration keeps the status index and leaves out the priority one. Every index takes space and has to be updated on every insert, so an index nothing uses is all cost.

**Write cost.** Inserting 100,000 tickets in a transaction that was rolled back took between 450 and 600 ms both with and without the two new indexes, varying from run to run (median of 7, three rounds each). At this size, the extra index work was smaller than the run-to-run noise on this laptop, so this measurement doesn't show a write cost.

**A side effect worth knowing.** Those rolled-back inserts grew the tickets indexes from about 8 MB to about 386 MB. Postgres writes every row before it knows whether the transaction will commit, and a rollback only marks the rows dead. `VACUUM` made the space reusable but didn't shrink the files. `REINDEX TABLE tickets` rebuilt the indexes (18 MB for the table and indexes together), and re-running `scripts/explain_list.py` gave the same results as the table above, within the usual noise.

## Stats endpoint with and without the Redis cache (Stage 2)

**What's measured.** `GET /stats` returns ticket counts by status, and the backlog (open or pending tickets) by priority. A cache miss runs two count queries in Postgres and saves the result in Redis for 60 seconds. A cache hit reads it back from Redis.

**Setup.** The same 100,000 tickets and machine as above, plus Redis 8.10.2 from Homebrew. The API ran under uvicorn on the same machine, with one worker.

**Method.** `uv run python -m scripts.measure_cache` sends 200 requests that miss (it deletes the cached counts before each one) and then 200 that hit, checking the `X-Cache` header on every response. Times are measured by the script, so they include the HTTP round trip on this machine. It was run twice.

| | Median | 95th percentile |
| --- | ---: | ---: |
| Miss (counted in Postgres) | 5.8 to 6.2 ms | 10.7 to 12.3 ms |
| Hit (read from Redis) | 0.7 to 1.0 ms | 1.3 to 1.5 ms |

A hit was **6 to 8.5 times faster** at the median.

The latency gain is real but small, because Postgres counts 100,000 rows in a few milliseconds with the status index. The bigger win is load. If 100 agents' dashboards each refreshed every 10 seconds, that would be 10 requests a second. Without the cache, Postgres would run 20 count queries a second. With it, Postgres runs 2 queries a minute (or whenever a ticket changes, from the next step on), however many agents are watching.

**Redis down.** With redis-py's default settings, every command against a stopped Redis retried with growing waits and took 2.7 seconds. The cache client turns retries off and times out after 0.5 seconds, so with Redis down a request skips the cache and is answered from Postgres. A test checks that it takes under half a second.

## Rate limit (Stage 2)

**Behavior.** Against the running API with the default limit of 60, 61 requests from one IP in a few seconds returned 60 `200`s and one `429` with `Retry-After: 60`. The IP's log in Redis then held exactly 60 entries, because the rejected request removed its own entry.

**Cost per request.** Every request now runs one Redis transaction before the endpoint does anything. With the limiter on (`RATE_LIMIT_PER_MINUTE=100000`, so nothing is rejected), `scripts/measure_cache.py` measured cache hits at 0.86 to 0.98 ms at the median over two runs, inside the 0.7 to 1.0 ms measured before the limiter existed. The limiter's cost is too small to see at this precision. Cache misses varied from 8 to 15 ms over the same two runs, which is noise on the machine rather than the limiter, since misses run the same limiter as hits.

## API image size and build time (Stage 4)

**Setup.** `infra/api.Dockerfile`, built with Docker Desktop 29.9.2 on an Apple M4 Pro (24 GB), for linux/arm64. The base image (`python:3.14-slim`) and uv's image were already downloaded.

**Size.** The image saved with `docker save` and gzipped is **92 MB**, close to what a registry stores and a server downloads. Unpacked, the dependencies take 216 MB. The biggest is the Twilio SDK at 51 MB, though the app only uses its signature check and TwiML. Then come SQLAlchemy (29 MB), uvloop (16 MB), and psycopg's bundled libpq (24 MB in all). (`docker image ls` shows 484 MB, but Docker Desktop's figure counts the image both compressed and unpacked.)

**Build time.** Timed with `docker build -q`, on a copy of the project, so the code changes below never touched the real files.

| Build | Time |
| --- | ---: |
| No layer cache (`--no-cache`), two runs | 15.2 s, 7.8 s |
| Nothing changed | 0.7 s |
| One line of app code changed, three runs | 0.5 to 0.6 s |

The dependencies sit in their own layer, built before any code is copied in. A code change leaves that layer as it was, so Docker only redoes the small code layers. `--no-cache` throws away the layers but not uv's download cache, which is mounted separately, so even those builds didn't download every package again. That cache was empty only before the very first build, which took 18 s.

## CI run time (Stage 4)

**Setup.** Two runs of `.github/workflows/ci.yml` on pushes to `main`, read from the GitHub API's job and step times (`gh api repos/Phanindra02/support-ai/actions/runs/<id>/jobs`) and the build logs (`gh run view --job <id> --log`). `lint` and `test` run on `ubuntu-latest` side by side, then `image` runs on `ubuntu-24.04-arm`.

| | Run 39691944975: empty layer cache | Run 39883091887: every layer cached |
| --- | ---: | ---: |
| Whole run, push to finish | 2 min 7 s | 1 min 56 s |
| `lint` | 12 s | 12 s |
| `test` (401 tests, with Postgres and Redis) | 59 s | 51 s |
| `image`, all of it | 58 s | 56 s |
| Build the image | 35 s | 33 s |
| ...of which `uv sync` | 4.6 s | cached |
| ...of which saving layers to the Actions cache | 14.8 s | 12.6 s |
| Push to GHCR | 6 s | 6 s |

The second run only changed `docs/ROADMAP.md`, which isn't in the build context, so every build step was `CACHED`. Even so, it saved just 2 s. On GitHub's runners, uv installs all 68 packages in under 5 seconds, so there's little for the cache to save. Most of the build is moving layers instead: loading the image into Docker for the smoke test, and saving every layer back to the cache (`cache-to: mode=max`) on each run. On the Mac the cache matters far more (0.6 s against 8 to 15 s, above), because Docker keeps the layers on the same disk.

A whole run takes about 2 minutes, and `test` then `image` are the critical path. Saving only the final layers (`mode=min`) or dropping the Actions cache could save 10 to 15 s a run. That hasn't been tried.

## PDF text Lambda (Stage 5)

**Setup.** One real upload through `POST /documents` on the EC2 server: a 40 KB, text-based PDF. The times come from the S3 objects' `LastModified`, the API's access log, and the Lambda's `REPORT` line in CloudWatch Logs (`aws logs tail /aws/lambda/support-ai-extract-text`). The function has 512 MB on `arm64`, Python 3.14. It was the function's first run, so Lambda had to start a new container (a cold start).

| | |
| --- | ---: |
| Upload in S3 to text saved in S3 | 2 s |
| Text pulled out | 2,167 characters |
| Container start (`Init Duration`) | 655 ms |
| The run itself (`Duration`) | 902 ms |
| Billed | 1,557 ms |
| Memory used | 121 of 512 MB |

Lambda now bills the container's start too, so a cold run is billed for both: 1,557 ms at 0.5 GB is 0.78 GB-seconds, about $0.00001 past the free allowance of 400,000 GB-seconds a month. A run that finds a warm container skips the 655 ms. The function used under a quarter of its memory, so 256 MB would likely do, but Lambda gives CPU in proportion to memory, so pypdf would run slower. Only one PDF was measured, so these are a first look, not a benchmark.

## Bot accuracy on 25 questions (Stage 6)

Measured on 2026-10-24 with `uv run python -m scripts.measure_bot`, against the three sample help docs and the 25 questions in `docs/help-docs/` (24 answered in the docs, 1 that isn't). It uses the real `text-embedding-3-small` and `gpt-4.1-mini` at temperature 0, the bot's own prompt, and the same cosine distance pgvector uses, in memory. About $0.02 a run.

- Retrieval: one of the chunks found holds the answer's key phrase (24 answerable questions).
- Answer: the reply holds that phrase, or says "I don't know." for the question the docs don't answer.
- Source: the bot names exactly the doc the answer is in, and none when it doesn't know.

| Chunk size | Chunks in all | Chunks per question | Retrieval | Answers | Sources |
|---|---|---|---|---|---|
| 1,000 (kept) | 12 | 4 | 24/24 | 25/25 | 25/25 |
| 1,000 | 12 | 2 | 24/24 | 25/25 | 25/25 |
| 500 | 23 | 4 | 24/24 | 25/25 | 25/25 |
| 500 | 23 | 2 | 24/24 | 25/25 | 25/25 |
| 1,500 | 9 | 2 | 24/24 | 25/25 | 25/25 |

The first run, before a fix, scored 24/25 on answers and sources. The bot replied to the Chicago question with "I don't know." and then "Excerpts used: None", which the bot didn't strip, so it counted as an answer. On WhatsApp, that customer would have been sent "I don't know" instead of getting a ticket. The fix accepts anything after "Excerpts used:".

Every setting scores perfectly, so this test set can't tell them apart: three short docs give only 9 to 23 chunks, and each question's answer is in one place. The settings stay at 1,000 characters and 4 chunks, which leaves room for answers that span two places in the docs. Telling them apart needs longer docs and questions whose answers span sections.
