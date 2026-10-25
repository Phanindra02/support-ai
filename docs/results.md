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

## The Stage 7 datasets

Measured on 2026-10-24 with `uv run python -m ml.explore`.

**Tickets:** Tobias Bueck's "Multilingual Customer Support Tickets" on Kaggle, file `aa_dataset-tickets-multi-lang-5-2-50-version.csv`, English rows only. The text is the subject, a blank line, then the body.

| | Rows | Share |
|---|---|---|
| medium | 6,618 | 40.5% |
| high | 6,346 | 38.8% |
| low | 3,374 | 20.7% |
| All | 16,338 | |

- No ticket is missing its body or priority, and no text appears twice. 3,838 rows in the whole file (both languages) have no subject, so their text is the body alone.
- Median 60 words, 95% at most 96, longest 172. The median is 60 words for every priority, so length alone can't tell them apart.
- Low is the rare class, about a fifth of the rows, so it gets extra weight in training (step 6).

**Tweets:** CrowdFlower's "Twitter US Airline Sentiment" on Kaggle, `Tweets.csv`.

| | Rows | Share |
|---|---|---|
| negative | 9,178 | 62.7% |
| neutral | 3,099 | 21.2% |
| positive | 2,363 | 16.1% |
| All | 14,640 | |

- No tweet is missing its text or label, but 213 texts appear more than once, mostly retweets. They're dropped before the split, so no tweet is in both the training and test sets.
- Median 19 words, longest 36. Negative tweets run longer (median 21 words) than neutral or positive ones (14).

## Stage 7 training and test sets

Measured on 2026-10-24 with `uv run python -m ml.prepare`. Cleaning lowercases each text, drops links, @handles, and HTML escapes like `&amp;`, then drops empty texts and repeats. A stratified split holds back 20% for testing, with seed 42.

| | Rows | After cleaning | Train | Test |
|---|---|---|---|---|
| Tickets | 16,338 | 16,338 | 13,070 | 3,268 |
| Tweets | 14,640 | 14,276 | 11,420 | 2,856 |

- Tickets: medium 40.5%, high 38.8%, low 20.7%, in both sets.
- Tweets: negative 63.5%, neutral 20.8%, positive 15.6% in training and 15.7% in testing. Cleaning dropped 364 tweets, more than the 213 exact repeats, because tweets that differed only in their @handle, link, or capitals became repeats.

## Priority model scores (Stage 7)

Every model is trained on the 13,070 training tickets and scored once on the 3,268 test tickets (see above). Macro F1 is the plain average of the three F1 scores, so the rare low class counts as much as the others. That makes it the number to compare.

### Baseline: TF-IDF and Logistic Regression

Measured on 2026-10-24 with `uv run python -m ml.train tickets baseline`, which took about 3 seconds on the M-series Mac. TF-IDF on words and word pairs seen in at least 2 tickets, then scikit-learn's `LogisticRegression` with its default settings.

| | Precision | Recall | F1 | Tickets |
|---|---|---|---|---|
| high | 0.666 | 0.707 | 0.686 | 1,269 |
| medium | 0.598 | 0.737 | 0.661 | 1,324 |
| low | 0.725 | 0.313 | 0.437 | 675 |
| Accuracy | | | 0.638 | 3,268 |
| Macro average | 0.663 | 0.586 | 0.594 | |

- Low-priority recall is 0.313: the model calls most low tickets medium or high, because low is the smallest class and guessing the bigger ones is safer. When it does say low, it's right 72.5% of the time.

### XGBoost on TF-IDF and text extras

Measured on 2026-10-24 with `uv run python -m ml.train tickets xgboost`, which took 36 seconds on the Mac's cores (206 seconds of CPU time). The same TF-IDF, plus four extras for each ticket: characters, words, `!` marks, and `?` marks. XGBoost with 300 trees, learning rate 0.1, depth 6.

| | Precision | Recall | F1 | Tickets |
|---|---|---|---|---|
| high | 0.660 | 0.680 | 0.670 | 1,269 |
| medium | 0.574 | 0.755 | 0.652 | 1,324 |
| low | 0.781 | 0.253 | 0.383 | 675 |
| Accuracy | | | 0.622 | 3,268 |
| Macro average | 0.671 | 0.563 | 0.568 | |

- XGBoost scores below the baseline on every F1, and low-priority recall drops further, to 0.253. TF-IDF gives tens of thousands of sparse columns, each a word that's in few tickets. A linear model weighs all of them at once, while each tree splits on only a few, so on text this is a common result.
- The extras don't rescue it: the exploration found the median ticket is 60 words at every priority, so length carries little signal.

### Tuned with cross-validation, with and without class weights

Measured on 2026-10-24 with `uv run python -m ml.tune tickets`, which took 5 minutes 24 seconds, nearly all of it XGBoost. Each setting is scored by 3-fold stratified cross-validation on the training tickets, by macro F1. The best setting is then refit on all 13,070 training tickets and scored once on the test tickets. "Weighted" gives each ticket a sample weight so every priority counts the same in total: a low ticket weighs about 1.9 times as much as a medium one.

| Model | Weighted | Best setting | Cross-validation macro F1 | Test macro F1 | Test low recall |
|---|---|---|---|---|---|
| Logistic Regression | no | C=30 | **0.641** | **0.709** | 0.599 |
| Logistic Regression | yes | C=30 | 0.639 | 0.706 | **0.640** |
| XGBoost | no | depth 6 | 0.532 | 0.568 | 0.253 |
| XGBoost | yes | depth 6 | 0.568 | 0.615 | 0.578 |
| Baseline (from above) | no | C=1 | | 0.594 | 0.313 |

Logistic Regression tried C of 0.3, 1, 3, 10, 30, and 100. XGBoost tried depths 3 and 6. A first run that stopped at C=10 picked 10, the edge of the grid, so the grid was widened until the best value sat inside it.

The chosen model is the cross-validation winner: unweighted Logistic Regression with C=30. On the test tickets:

| | Precision | Recall | F1 | Tickets |
|---|---|---|---|---|
| high | 0.735 | 0.756 | 0.745 | 1,269 |
| medium | 0.700 | 0.747 | 0.723 | 1,324 |
| low | 0.732 | 0.599 | 0.659 | 675 |
| Accuracy | | | 0.720 | 3,268 |
| Macro average | 0.723 | 0.700 | 0.709 | |

- Tuning C did most of the work: macro F1 rose from 0.594 to 0.709 and low recall from 0.313 to 0.599. The default C=1 held the weights too small for this many word features.
- Once C is tuned, weights add little to Logistic Regression: low recall rises to 0.640, but the other labels lose about as much. The cross-validation scores differ by 0.002, which is noise, so the plain model stays.
- Weights help XGBoost much more (low recall 0.253 to 0.578), but it still trails Logistic Regression by about 0.09 macro F1 and trains 100 times slower.
- Every test score is about 0.07 above its cross-validation score. Each cross-validation model learns from two thirds of the training tickets (about 8,700), while the final one learns from all 13,070, so the model is still getting better with more data.

## Sentiment model scores (Stage 7)

Measured on 2026-10-24 on the 11,420 training tweets and 2,856 test tweets (see the split above). The baseline came from `uv run python -m ml.train tweets baseline`, and the tuning from `uv run python -m ml.tune tweets`, which took 1 minute 34 seconds. The models, grids, folds, and weights are the same as for the priority model.

| Model | Weighted | Best setting | Cross-validation macro F1 | Test macro F1 | Test recall: negative / neutral / positive |
|---|---|---|---|---|---|
| Baseline | no | C=1 | | 0.716 | 0.963 / 0.450 / 0.611 |
| Logistic Regression | no | C=30 | 0.707 | 0.738 | 0.921 / 0.550 / 0.685 |
| Logistic Regression | yes | C=1 | **0.730** | **0.743** | 0.847 / 0.662 / 0.749 |
| XGBoost | no | depth 6 | 0.708 | 0.724 | 0.913 / 0.539 / 0.653 |
| XGBoost | yes | depth 6 | 0.716 | 0.712 | 0.778 / 0.697 / 0.745 |

The chosen model is the cross-validation winner: weighted Logistic Regression with C=1. On the test tweets:

| | Precision | Recall | F1 | Tweets |
|---|---|---|---|---|
| negative | 0.888 | 0.847 | 0.867 | 1,814 |
| neutral | 0.594 | 0.662 | 0.626 | 595 |
| positive | 0.722 | 0.749 | 0.735 | 447 |
| Accuracy | | | 0.793 | 2,856 |
| Macro average | 0.735 | 0.753 | 0.743 | |

- Here weights matter, unlike for the tickets: negative is 63.5% of the tweets, so without weights the model leans toward calling things negative. That gives high negative recall (0.963 for the baseline) but low precision (0.805). Weighting trades some negative recall for precision of 0.888, so fewer calm customers get flagged as angry, and neutral recall rises from 0.450 to 0.662.
- Accuracy drops slightly (0.801 to 0.793), because the weighted model stops leaning on the biggest class. Macro F1 is what was chosen on.
- Neutral is the hardest label: many neutral tweets are questions that use the same words as complaints.
- XGBoost comes much closer here than on the tickets, and the whole tuning run took 1.5 minutes against 5.5, since tweets are short and give far fewer word features.

## Saved models (Stage 7)

Measured on 2026-10-24 with `uv run python -m ml.save --upload`, which trains both chosen models in about 4 seconds and uploads them to `s3://support-ai-docs-phanindra02/models/`.

| Model | Version | Test macro F1 | model.joblib |
|---|---|---|---|
| priority | v1 | 0.709 | 2.4 MB |
| sentiment | v1 | 0.743 | 1.1 MB |

- Each model is trained on its training set only, so the test score in its `metadata.json` belongs to that exact file.

## Model service image (Stage 7)

Measured on 2026-10-24 with `docker image ls support-ai-model` after `docker compose build model` on the Mac, and `du -sm` inside the image.

- The image is 659 MB. Its virtual environment is 335 MB: SciPy 149 MB and NumPy 69 MB (each with its bundled libraries), scikit-learn 57 MB, and botocore 31 MB. scikit-learn needs SciPy and NumPy, so most of the size comes with the models' one library.
- It installs only the `service` dependency group, without the API's packages, and copies only four files from `ml/`.
- Started with `docker compose up -d model`, it was healthy within a few seconds and answered `/predict/priority` from inside the container.

## Model service on the server (Stage 7)

Measured on 2026-10-25 on the t4g.small server (2 GB), a minute after the deploy of PR #32 started the model container from ECR. Read with `docker stats --no-stream` and `free -m` through SSM.

- The container was healthy within a minute, after downloading both models from S3 into its `model-cache` volume. It used 149 MB, less than the 268 MB it used idle on the Mac.
- With it running, the server had 736 MB available, down from 817 MB the day before (the stack also restarted, so not all of the difference is the model).
- From inside the container, "Our production server is down and customers cannot log in" came back high priority (0.93), and "This is the third time I am asking, absolutely useless service" came back negative (0.66). Both from v1.

## Priority prediction job (Stage 7)

Measured on 2026-10-24 with the compose stack on the Mac (`docker compose up -d --build api worker model`). From the API container, a ticket was saved ("Production outage" / "Our whole site is down and no customer can pay...") and its prediction queued with `queue_priority_prediction`.

- The ticket was created at medium, and 4 seconds later it was high.
- The worker's log says `worker.jobs.predict_priority(1)` completed in 0.66 seconds, which covers reading the ticket, calling the model container, and saving the priority.

## "Very negative" threshold (Stage 7)

Measured on 2026-10-24 by scoring the 2,856 test tweets with the saved sentiment model (`models/sentiment/v1`). A tweet counts as flagged when the model's "negative" score is at least the threshold. Precision is the share of flagged tweets that really are negative. Recall is the share of negative tweets that get flagged.

| Threshold | Flagged | Share of all tweets | Precision | Recall |
|---|---|---|---|---|
| 0.5 | 1,529 | 53.5% | 0.914 | 0.771 |
| 0.6 | 1,242 | 43.5% | 0.947 | 0.648 |
| 0.7 | 907 | 31.8% | 0.968 | 0.484 |
| **0.8** | **580** | **20.3%** | **0.991** | **0.317** |
| 0.9 | 244 | 8.5% | 0.992 | 0.133 |
| 0.95 | 92 | 3.2% | 0.978 | 0.050 |

- 0.8 was chosen. Almost every flagged message is really negative (99.1%), and only the angriest fifth of messages alert. At 0.5, over half of all messages would alert, which trains a team to ignore the channel. Above 0.8, precision stops rising while recall keeps falling.
- On four made-up support messages, the alert reasons came out as expected: no alert for "Where is my order?" or a password question, and an alert for an angry refund request (negative 0.94) and for a site outage (high priority 0.97). The outage also scored negative 0.82, though it's written calmly. A model trained on airline tweets reads outage words as anger, which is a cost of training on another domain's data.

## Kubernetes on minikube (Stage 8)

Measured on 2026-10-25 on minikube 1.39 (Kubernetes 1.37, 4 CPUs and 4 GB from Docker Desktop), with the chart installed by `helm install support-ai infra/k8s/support-ai --wait`. Postgres was the Mac's, at `host.minikube.internal`.

- `kubectl top pods`, two minutes after the install with no traffic: each API pod 155 MB and 3 to 14 millicores, the model service 130 MB, the worker 73 MB, and Redis 13 MB. All are well inside their limits (512, 768, 512, and 256 MB), and the autoscaler read the API at 3% of its 70% target.
- From an API pod, a ticket ("Production outage" / "Our whole site is down and no customer can pay. This is urgent.") was saved at medium and its triage queued. 2 seconds later it was high, with both predictions saved from v1. The worker's log says the job took 1.6 seconds, which covers calling the model service and saving. An earlier, angrier ticket took 2.1 seconds and stayed medium (0.61), with negative 0.73, below the alert's 0.8.
- `helm upgrade` with nothing changed took 5.3 seconds: the migrate Job ran first, and no pod was replaced.
- The worker restarted once on the first install, because it started before Redis accepted connections.

## EKS (Stage 8)

Measured on 2026-10-25 on the EKS cluster from `infra/k8s/eks-cluster.yaml`: Kubernetes 1.37, two t4g.medium nodes in us-east-2, with the chart installed from `values-eks.yaml` and CI's images for `sha-9b30d69`.

- `eksctl create cluster` took about 20 minutes, which is mostly EKS building the control plane. The two nodes then used 3% of their CPU and 509 to 556 MB of their 4 GB each, before the chart.
- The first install failed: Redis crashed with "Can't open or create append-only dir appendonlydir: Permission denied". Redis runs as user 999, and a new EBS disk's top folder belongs to root. minikube's disks let anyone write, so it hadn't shown there. With `fsGroup: 999` on the pod, `helm upgrade` finished in 46 seconds, including the migrations against RDS.
- `kubectl top pods`, a few minutes later with no traffic: the API pods 169 and 207 MB, the model service 146 MB, the worker 79 MB, and Redis 6 MB. About the same as on the EC2 server and minikube.
- From an API pod, without writing anything: it read RDS, with `DATABASE_URL` and the JWT key from Parameter Store, and got a prediction from the model service through its Service. No AWS keys were in its environment, only Pod Identity's credentials address. The model service's role was refused Parameter Store (`AccessDeniedException`).
- The load balancer for the Ingress was reconciled about 10 seconds after `helm upgrade`, and answered a couple of minutes later, once its DNS name resolved. Asked for `support.mysolospark.com` at its address (before the domain moved), HTTP gave a 301 to HTTPS, and HTTPS gave `/health`'s 200 with Amazon's certificate for the domain, valid until May 2027. Six requests from the Mac took 0.09 to 0.18 seconds each, including a new TLS connection every time.

## Load test and autoscaling on EKS (Stage 8)

Measured on 2026-10-25 against `https://support.mysolospark.com` on the EKS cluster (two t4g.medium nodes, CI's images for `sha-8c9f97b`), with Locust 2.47 on the developer's Mac at home, so every time includes the round trip from home to us-east-2.

**Method.** `scripts/locustfile.py`: 200 simulated agents, added 5 a second, for 8 minutes. Each logs in once, then about once a second reads `/stats` (3 in 8), `/tickets` (3 in 8), `/tickets?status=open` (1 in 8), or `/health` (1 in 8). The API's per-address rate limit was raised to 100,000 a minute for the test, since every request came from one address, then put back to 60. The autoscaler was read every 15 seconds with `kubectl get hpa api`.

- **Throughput:** 87,244 requests, about 182 a second.
- **Latency, all requests:** median 34 ms, 95th percentile 130 ms, 99th 420 ms, slowest 1.7 s. `/tickets`, which reads Postgres every time, was 36 / 140 / 400 ms. `/stats`, mostly from the Redis cache, 33 / 130 / 380 ms. Logging in was 890 ms at the median, because hashing a password is slow on purpose.
- **Errors:** 1 of 87,244, a 502 on `/stats` 9 seconds into the scale-up, while the load balancer was adding the new pods.
- **Autoscaling:** the API started at its minimum of 2 pods. 30 seconds after the load began, their CPU was at 212% of the 250m request, and the HPA asked for 4 pods. 30 seconds later it was at 309% and the HPA went to 6, its maximum. All 6 were ready within about a minute of the load starting. For the remaining 7 minutes, CPU held at 74% to 78% of the request, just over the 70% target, so 6 pods were about right for 182 requests a second. More load would need a higher `maxReplicas`, and past about 8 API pods, a third node.
