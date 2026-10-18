# Results

Every number measured in this project, and how it was measured, so each one can be checked and repeated.

## Ticket list query on 100,000 tickets (Stage 2)

**Data.** 100,000 seeded tickets from 49,890 customers, with 232,455 messages (`uv run python -m scripts.seed --reset --count 100000 --seed 42`). 92.8% are closed, 4.7% open, and 2.5% pending. 7% are urgent, and 235 are both open and urgent. The tickets table is 15 MB.

**Machine.** An Apple M4 Pro with 24 GB of memory, running PostgreSQL 16.15 from Homebrew with default settings: `work_mem` 4 MB, `shared_buffers` 128 MB, and up to 2 parallel workers per query.

**Method.** `uv run python -m scripts.explain_list` runs `EXPLAIN (ANALYZE, BUFFERS)` on the exact query `GET /tickets` builds (`ticket_list_query` in `app/main.py`), with `limit=20`. Each request runs once to warm the cache, then 7 more times, and the table shows the median "Execution Time". That's time inside Postgres only, without the network or Python. Repeated runs differ by about 10%.

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
