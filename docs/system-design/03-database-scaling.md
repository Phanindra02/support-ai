# How databases scale

What to do as the tickets table grows and more people read it, in roughly the order you'd do it: index, scale up, pool connections, add read replicas, partition, and only then shard. Plus when a NoSQL database is the better fit.

## Indexes come first

An index is a sorted copy of one or more columns, with a pointer from each entry to its row. Postgres's default index is a **B-tree**: a shallow tree of sorted pages, so finding any value takes a few page reads, even among millions of rows. Without an index, Postgres reads the whole table (a sequential scan).

This project measured it on 100,000 tickets (see [results](../results.md)):

| Request | No index | Index on `(created_at, id)` |
| --- | ---: | ---: |
| First page, newest first | 11.07 ms | 0.02 ms |
| `skip=50000` | 22.19 ms | 7.59 ms |

The first page got about 550 times faster because the index is already in the order the list sorts by. Postgres reads it backward from the newest entry and stops after 20 rows. The deep page only got 3 times faster, because `OFFSET` still walks past 50,000 rows. An index can't fix that, but cursor pagination can (see the [REST notes](02-rest-api-design.md)).

What decides whether an index helps:

- **Column order matters in a multi-column index.** `(created_at, id)` works like a phone book sorted by last name, then first name. It helps a search on `created_at`, or on `created_at` and `id` together, but not on `id` alone. This is the "leftmost prefix" rule.
- **Selectivity.** An index pays off when a query keeps a small share of rows. `status = 'open'` matches 4.7% of tickets, so an index on status helps count them. `status = 'closed'` matches 93%, so reading the whole table is cheaper than jumping between index and table for nearly every row. The planner makes this choice from table statistics, which `ANALYZE` refreshes.
- **Index-only scans.** If the index holds every column a query needs, Postgres can answer from the index alone. Counting tickets by status reads only `ix_tickets_status`.
- **Partial indexes** cover just the rows that matter, like `CREATE INDEX ... WHERE status <> 'closed'`. That would index only the 7% of tickets still open or pending, a much smaller index for the backlog count.

What an index costs:

- **Every insert and update must change every index on the table.** More indexes means slower writes, and the indexes take space.
- **So measure, don't guess.** `EXPLAIN ANALYZE` shows which index a query used. `pg_stat_user_indexes` counts how often each index is used. This project tried an index on priority, saw it used 0 times, and left it out.
- **Postgres doesn't index foreign keys automatically.** Without `ix_tickets_customer_id`, finding a customer's tickets, or deleting a customer, would read every ticket.
- **Building an index on a busy table.** A plain `CREATE INDEX` blocks writes until it finishes. `CREATE INDEX CONCURRENTLY` doesn't, but it's slower and can't run inside a transaction.

A side note on how Postgres stores rows (MVCC). An update or delete doesn't overwrite a row. It writes a new version and marks the old one dead, so readers never wait for writers. `VACUUM`, which autovacuum runs in the background, cleans up dead rows. This project saw the cost: about 100 benchmark inserts of 100,000 rows each, all rolled back, grew the ticket indexes from 8 MB to 386 MB until a `REINDEX`.

## Scale up before scaling out

**Vertical scaling** means a bigger machine: more CPU, more memory so more of the data stays cached, faster disks. On RDS (Stage 5), that's changing the instance size, with a few minutes of downtime. It needs no code changes, and one Postgres server handles far more than this project will. Its limits are price (the biggest machines cost much more per unit of power) and a ceiling: one machine is still one machine, and one point of failure.

**Connection pooling** usually matters before raw size. Each Postgres connection is a separate server process using several megabytes, and the default limit is 100 connections. SQLAlchemy keeps a pool of 5 connections per process, plus up to 10 extra under load, so 4 uvicorn workers in each of 5 Kubernetes pods (Stage 8) could ask for up to 300 connections. A pooler such as PgBouncer or RDS Proxy sits in between and shares a few real connections among many clients.

## Read replicas spread the reads

A **read replica** is a copy of the database that follows the primary by replaying its write-ahead log, the record of every change. All writes go to the primary, and reads can go to any replica.

- **Good for read-heavy work.** Support dashboards mostly read: lists, counts, conversations. Replicas can take those reads off the primary.
- **Replication lag.** Replicas usually trail the primary by milliseconds, but sometimes by seconds. An agent who closes a ticket and then refreshes a list served by a replica might see it still open. The usual fix is "read your own writes": send a user's reads to the primary for a short time after they write.
- **Failover.** If the primary fails, a replica can be promoted to primary. RDS Multi-AZ keeps a standby in another availability zone for exactly this, but that standby doesn't serve reads. Read replicas are a separate feature.

## Partitioning splits a table, and sharding splits the data across servers

**Partitioning** splits one big table into smaller ones on the same server, for example one per month of `created_at`. Queries that filter by date only read the partitions they need, and archiving a year of old tickets is a quick `DROP` of its partitions instead of a slow `DELETE`. Postgres supports this natively.

**Sharding** splits the data across several database servers, each holding some of the rows. It's the step for when one primary can't take the writes or hold the data.

- **The shard key decides everything.** It should keep data that's used together on the same shard, and spread the load evenly. If this became a product with many client companies, `company_id` would be the natural key: each company's customers, tickets, and messages would sit together.
- **Hot shards.** One huge company would overload its shard. Hashing the key spreads keys evenly, but it doesn't help when a single key is too big.
- **Cross-shard queries are slow and complicated.** Platform-wide stats would have to ask every shard and combine the answers. Foreign keys and transactions don't work across shards.
- **Resharding is painful.** Adding shards means moving data. Consistent hashing reduces how much has to move.

Sharding is a last resort, after indexes, a bigger machine, caching, and replicas run out.

## SQL or NoSQL

| Kind | Examples | Good at | Weak at |
| --- | --- | --- | --- |
| Relational (SQL) | Postgres, MySQL | Joins, constraints, transactions, flexible queries | Scaling writes beyond one primary |
| Key-value | Redis, DynamoDB | Very fast lookups by key, huge scale | Queries other than by key |
| Document | MongoDB | Records with nested data and changing shapes | Joins across documents |
| Wide-column | Cassandra | Very high write volume across many servers | Ad hoc queries, transactions |
| Search | Elasticsearch, OpenSearch | Full-text search and ranking | Being the source of truth |

Postgres fits this project because its data is relational, and it relies on what relational databases are good at:

- **Constraints keep the data correct.** A unique phone number makes two simultaneous messages from a new customer end up as one customer, not two.
- **Transactions and joins.** Opening a ticket saves the customer, the ticket, and the first message together, or none of them. The list joins tickets to customers.
- **Flexible queries.** The stats counts and the weekly report needed no new tables.
- **pgvector (Stage 6)** keeps document embeddings in the same database, so there's no separate vector store to run.

The project already uses NoSQL where it fits: Redis, a key-value store, holds the cache and the rate limit logs, which are short-lived, looked up by key, and safe to lose.

## How far this setup goes

At 100,000 tickets, the database holds about 65 MB: 18 MB of tickets and their indexes, 40 MB of messages, and 7 MB of customers. Ten million tickets would be about 6.5 GB, which still fits comfortably on one Postgres server and mostly in memory. The first limits this project would hit are database connections, as the API scales out in Stage 8, and dashboard reads, which the stats cache already takes off the database. Next would come a pooler and a read replica. Sharding wouldn't be needed for a very long time.

## Interview questions

- Your list query is slow. How do you find out why, and what do you check first?
- Why does the order of columns in a multi-column index matter?
- Why might Postgres ignore an index you added?
- What does each index cost you?
- What is replication lag, and how would it confuse a user?
- What's the difference between partitioning and sharding? How would you choose a shard key for this app?
- Why Postgres instead of MongoDB for tickets and messages?
- Your API scales to 20 pods and the database starts refusing connections. What happened, and what's the fix?
