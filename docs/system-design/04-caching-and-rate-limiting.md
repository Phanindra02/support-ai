# Caching and rate limiting

Two jobs Redis does for this API: remembering answers so the database doesn't work them out again, and counting requests so no client can flood the API. Both sit in front of the database, and both have to work when several copies of the API run at once.

## Why cache

A cache keeps a copy of a result somewhere faster to read than where it came from. It helps when a result is:

- **Read much more often than it changes.** Dashboard counts are read on every refresh, but change only when a ticket does.
- **Costly to work out.** Counting 100,000 tickets takes milliseconds of database work, and that adds up when many dashboards refresh.
- **Fine to be slightly out of date,** or easy to refresh when the data changes.

Caches sit at every level: the browser (`Cache-Control` headers), a CDN near the user, the app's own memory, a shared cache like Redis or Memcached, and the database's own memory of recently read pages.

**In-process memory or a shared cache?** A Python dictionary is the fastest cache there is, but each uvicorn worker and each Kubernetes pod would have its own copy. One pod could clear its copy after a ticket changes while the others keep serving the old counts. A shared cache like Redis costs a network round trip, under a millisecond on the same machine, but every copy of the API sees the same data.

## Caching patterns

**Cache-aside (lazy loading).** The app checks the cache. On a miss, it reads the database and puts the result in the cache. The cache only ever holds what someone asked for, and if the cache goes down, the app can still read the database. The cost is that the first request after a miss is slow, and the app code has to handle both paths.

**Read-through.** Like cache-aside, but the cache itself loads from the database on a miss, so the app only talks to the cache. It needs a cache that knows how to reach the database, as some managed caches and libraries do.

**Write-through.** Every write goes to the cache and the database together, so the cache is always current for anything written. Writes get slower, and the cache fills with data that may never be read. It suits single records, like "ticket 42", more than results computed from many rows, like counts.

**Write-behind (write-back).** Writes go to the cache, and the cache saves them to the database later, in batches. Writes are very fast, but anything not yet saved is lost if the cache fails. It suits counters such as page views, not tickets.

**Refresh-ahead.** The cache reloads popular entries just before they expire, so readers rarely see a miss.

## Keeping the cache correct

Phil Karlton's line, "There are only two hard things in computer science: cache invalidation and naming things", is about this part.

- **Expiry (TTL).** Every entry deletes itself after a while. Simple, and it limits how stale data can get, but data is stale until then.
- **Delete on write.** When the data changes, delete the cached entry, and the next read loads the new value. Deleting is safer than writing the new value into the cache: if two updates race, the cache can end up with the older one, while a delete just forces a fresh read.
- **Delete after the commit, not before.** Deleting first leaves a gap: another request can read the old row and cache it again before the new one is saved.
- **The race that remains.** A reader loads the old value, a writer commits and deletes the cache entry, and then the reader puts its old value back. The entry stays stale until it expires. Keeping a TTL on every entry, even ones cleared on write, puts a limit on how long such a mistake lasts.

## Eviction: what goes when memory runs out

A cache has limited memory, so something has to go.

- **LRU (least recently used):** remove what hasn't been read for the longest. The usual default.
- **LFU (least frequently used):** remove what's read least often. Better when a few keys stay popular for a long time.
- **FIFO:** remove the oldest entry, whether it's popular or not.
- **TTL-first:** remove entries closest to expiring.

Redis chooses with `maxmemory-policy`. The Homebrew Redis on the developer's Mac has no memory limit and the policy `noeviction`, so once memory filled up, writes would start failing instead of old keys being removed. A production Redis needs a `maxmemory` and a policy such as `volatile-lru`, which only removes keys that have an expiry. Every key this project writes has one.

## Problems a cache brings

- **Cache stampede (thundering herd).** A popular entry expires, and hundreds of requests miss at the same moment and all hit the database. Fixes: let one request recompute while the others wait or get the old value (a lock, or "single flight"), refresh popular entries early, or add a little randomness to TTLs so they don't all expire together.
- **Cache penetration.** Requests for things that don't exist always miss and always reach the database. Fix: cache the "not found" result too, briefly.
- **Hot keys.** One key read so often that it overloads a single cache server. Fix: keep extra copies, or a small in-process cache in front.
- **The cache going down.** If the app depends on the cache, a cache outage becomes an app outage. The app should fall back to the database, and the database must be able to take that load for a while.

## Why rate limit

A rate limit caps how many requests a client can make in some period. It protects against:

- **Abuse and attacks,** like scripts guessing passwords or scraping data.
- **Accidents,** like a client stuck in a retry loop.
- **Costs.** From Stage 6, a question to the bot calls a paid LLM API, so an unlimited client could run up the bill.
- **Unfairness.** One busy client shouldn't slow the API down for everyone else.

Limits are keyed by something that identifies the client: the IP address, a user id after login, or an API key. Each has drawbacks. Many people can share one IP address (an office, or mobile users behind a carrier's network), and logins and keys only exist once there's authentication.

## Rate-limiting algorithms

**Token bucket.** Each client has a bucket of tokens that refills at a steady rate, up to a maximum. A request takes a token, and an empty bucket means a 429. It allows short bursts up to the bucket size while holding the average to the refill rate. It stores two numbers per client, the tokens left and the last refill time, and updating them safely in Redis takes a small Lua script. AWS API Gateway and Stripe use token buckets.

**Leaky bucket.** Requests join a queue that's processed at a fixed rate. If the queue is full, new requests are dropped. It smooths traffic into a steady stream, which suits protecting something that can only take work at a fixed pace.

**Fixed window counter.** Count requests per client per calendar minute: `INCR ratelimit:{ip}:{minute}`, with an expiry. It's the simplest and uses the least memory. Its flaw is the boundary: a client can send a full limit at 0:59 and another at 1:00, which is twice the limit in two seconds.

**Sliding window log.** Store the time of each request, for example in a Redis sorted set. On each request, drop times older than the window and count what's left. It's exact, with no boundary burst. It stores one entry per request in the window, so memory grows with the limit: fine for 60 a minute, wasteful for 10,000 a second.

**Sliding window counter.** Keep fixed-window counts for the current and previous windows, and estimate with a weighted sum. Fifteen seconds into a minute, the estimate is the current count plus 75% of the previous minute's. It uses as little memory as a fixed window and smooths most of the boundary burst, but it's an estimate. Cloudflare uses this.

## Rate limiting across many servers

- **The counts must be shared.** With several copies of the API, each counting on its own, a client could get the limit once per copy. A shared store like Redis gives every copy the same count.
- **The update must be atomic.** Two requests at once must not both read 59 and both go through as the 60th. Redis runs a `MULTI`/`EXEC` block or a Lua script as one step, with no other client's commands in between.
- **It costs a round trip per request.** Every request now waits for Redis, under a millisecond on the same machine or the same network.
- **If the store is down: fail open or fail closed?** Failing open lets everything through, so the API stays up but unprotected. Failing closed rejects everything: protected, but down. For a general API, open is usual. For a login endpoint under attack, closed can be the right call.

## Telling the client

- **429 Too Many Requests** is the status code for "slow down".
- **`Retry-After`** says how many seconds to wait. Well-behaved clients wait that long before retrying.
- **Allowance headers** let clients see how much is left before they hit the limit. GitHub sends `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`, and an IETF draft is standardizing `RateLimit` headers for the same purpose.

## What this project uses, and why

### The stats cache: cache-aside, cleared on every ticket change, with a 60-second expiry

`GET /stats` looks for the key `stats:tickets` in Redis. On a miss, it counts tickets in Postgres and saves the result for 60 seconds. Creating or updating a ticket deletes the key after the database commit, and so does the seed script. The code is in `app/routers/tickets.py` and `app/cache.py`.

- **Why cache-aside.** The counts are worked out only when someone asks, so nothing is computed while nobody's watching a dashboard. If Redis goes down, the endpoint counts in Postgres instead of failing.
- **Why not write-through.** Every ticket change would have to recount all tickets as part of the write, whether or not anyone reads the counts, which slows down the path that matters most. Keeping running totals with `INCR` instead would make a second copy of the truth, and it would drift as soon as a change skipped the API, like the seed script or a manual fix in SQL. Deleting and recounting can't drift.
- **Why delete on write and also expire.** Deleting makes the counts current right after a change. The 60-second expiry is the safety net: it limits how long the counts can stay wrong if a delete fails while Redis is briefly unreachable, if a change skips the API, or if the race described above happens.
- **Why Redis instead of a Python dictionary.** Several uvicorn workers, and in Stage 8 several pods, must all see the same cache and the same deletes.
- **What it bought.** A hit takes about 1 ms and a miss about 6 ms (see [results](../results.md)). The bigger win is load: however many dashboards poll, Postgres counts at most once a minute, plus once after each change.

### The rate limit: a sliding window log, 60 requests a minute per IP, failing open

Each IP has a sorted set of its request times. A request drops entries older than 60 seconds, adds itself, and counts, all in one `MULTI`/`EXEC` transaction. Over 60, it takes itself back out and gets a 429 with `Retry-After`. The code is in `app/rate_limit.py`.

- **Why a sliding window log.** The promise is simple: 60 requests in any 60 seconds, with no loophole at the minute boundary. The log keeps that promise exactly. Its weakness is memory, one entry per request, but at a limit of 60 that's at most 60 small entries per IP.
- **Why not a fixed window.** It allows 120 requests in two seconds around the turn of each minute.
- **Why not a token bucket.** A token bucket deliberately allows bursts, and updating it atomically needs a Lua script. It would be the better choice to let a client make 10 quick requests at once, as a page load might, while capping the average. That's not the promise here.
- **Why not a sliding window counter.** It saves memory by estimating, which only matters at much higher limits.
- **Why rejected requests don't count.** A client that's over the limit and waits for `Retry-After` gets its full allowance back. If rejected requests counted, a client retrying too fast would stay blocked forever.
- **Why fail open.** Without Redis, requests go through unlimited, and a warning is logged. For a support API, staying up matters more than the limit during a short Redis outage. Stage 3's login endpoint is a case for failing closed instead, because that's where password-guessing attacks go.
- **Why by IP.** There are no logins yet. Stage 3 adds agent logins, which allow a limit per agent.

### What to change in later stages

- **Behind a proxy (Stage 5),** every request seems to come from Caddy's address, so every user would share one limit. uvicorn must run with `--proxy-headers` and `--forwarded-allow-ips` set to the proxy's address, so the real client IP comes from `X-Forwarded-For`, trusted only when the proxy sent it.
- **The Twilio webhook (Stage 3)** receives every customer's WhatsApp messages from Twilio's own servers, so a per-IP limit would throttle all customers together. That endpoint should skip the IP limit, or limit by the customer's phone number instead.
- **One Redis does two jobs.** Losing a cache entry costs one recount, but losing a rate-limit log resets that IP's limit. A production Redis should set `maxmemory` with `volatile-lru`, which only evicts keys that have an expiry. Every key here has one, so this works, but a larger system might give the rate limiter its own Redis.

## Interview questions

- When is a cache the wrong answer?
- Compare cache-aside and write-through. Which fits a single ticket, and which fits counts across all tickets?
- Why delete a cache entry on a write instead of writing the new value?
- What is a cache stampede, and how do you prevent one?
- What happens to your app if Redis goes down?
- Compare the token bucket and the sliding window log. When would you pick each?
- What's wrong with a fixed window counter?
- How do you rate limit correctly across 10 copies of the API?
- Fail open or fail closed? Does the answer change for a login endpoint?
