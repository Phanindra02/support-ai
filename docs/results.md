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
