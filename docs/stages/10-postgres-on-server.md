# Stage 10: Postgres on the server instead of RDS

**Status:** the app runs on the server's Postgres container, a backup goes to S3 every night, and the newest one has been restored and checked. RDS is gone, along with its subnet group and security group, which the first teardown had missed.

## What was built

- **Postgres back on the server.** `infra/aws/compose.ec2.yml` starts the Postgres container from `docker-compose.yml` again, on a volume of its own (`server-postgres-data`). The app connects with the URL `docker-compose.yml` builds for that container. The secrets still come from Parameter Store, except the database's password, `POSTGRES_PASSWORD`, which stays in the server's `.env`.
- **A nightly backup.** At 08:00 UTC, cron runs `infra/aws/backup-database.sh`. It runs `pg_dump --format=custom` into a temporary file, then `scripts/upload_backup.py` (in the API's image, which already has boto3) uploads it to `backups/support-<UTC time>.dump` in the docs bucket.
- **Backup permissions and expiry.** The server's role gets `SaveDatabaseBackups`: it may add files under `backups/`, but not read, list, or delete them. `infra/aws/docs-bucket-lifecycle.json` deletes backups after 14 days, and cleans up multipart uploads that stopped halfway after a day.
- **Two ways to restore**, in `infra/aws/README.md` under "Restore a backup": a drill into a scratch database that compares every table's row count and the migration with the real database, and a restore over the real database for when only a backup can undo a mistake.
- **The move.** The server's empty database was replaced with a dump of the Mac's seed data, and the row counts matched. The first backup went to S3, and the drill restored it in 0.64 seconds with every count matching (`docs/results.md`).
- **The contract.** The `DATABASE_URL: !reset null` lines are gone, so nothing on the server can point the app anywhere but the container. `docs/rollback.md`, the design doc, the README, and the AWS README now describe a database on the server, and the AWS README keeps RDS's setup as a record.

### How the plan changed

The plan was **expand and contract**. First, start the container next to RDS, with nothing using it (PR #50). Then copy RDS into it, compare row counts, switch the app with one line in `.env`, and keep RDS for a week as the way back. Then remove what was only there for the move.

Before the move ran, the project's AWS resources had all been deleted (2026-10-26), once the roadmap was finished. When the account was rebuilt on 2026-11-10, RDS was left out on purpose. So there was nothing to copy from, and the container was filled from a seed dump instead. The copy from RDS, the backup, and both kinds of restore had been tried earlier on two local Postgres 16 clusters standing in for RDS and the container. Those commands are in `git show fec4ced:infra/aws/README.md`.

## Key decisions, and why

- **Leave RDS.** It was about $14 a month, the biggest line on the bill, for a database of 9 MB. The trade is recovery: RDS could restore to any moment of the last 7 days, up to about 5 minutes ago (RPO, the data you can lose, of about 5 minutes). A nightly dump can lose up to a day. For a portfolio project with seed data, a day is fine. For a real support team it might not be, and the RPO can be brought down cheaply (below).
- **`pg_dump` in custom format.** It reads one consistent snapshot while the app keeps writing (Postgres's MVCC), it's compressed, and `pg_restore` can restore all of it or single tables. At this size, a logical dump is simpler than disk snapshots or WAL archiving.
- **Dump to a file, then upload.** A `pg_dump | upload` pipe would upload half a backup if `pg_dump` failed halfway. The script has `set -euo pipefail`, so a failed dump stops it before the upload. The uploader also refuses anything that doesn't start with `PGDMP`, the custom format's first bytes, such as an empty file.
- **The server can add backups but not read or delete them.** If the server were broken into, the attacker couldn't download the database's history or wipe the backups. A server that can't read backups also can't restore one, so restores go through the Mac, as an admin.
- **Restores are all or nothing.** `--single-transaction` means a restore that fails partway leaves nothing behind. `--no-owner --no-privileges` leaves out who owned what, so a dump restores into any database whatever its users are called.
- **Check every table, not one.** The drill compares the migration and the row count of all seven tables, so a restore that silently dropped one table would show up.
- **The password stays in `.env`.** It opens only a database whose port isn't published, and whose data is on the same disk, so reading the file gives nothing that access to the server doesn't. Keeping it in Parameter Store would add a step before every `docker compose` command.
- **Keep RDS's setup as a record.** `rds-instance.json`, the certificate bundle in the image, and the AWS README's RDS section stay, marked as a record, with their tests. They explain Stage 5 to Stage 10, and taking the bundle out of the image is a small cleanup for later.

## How to run and demo it

```bash
# On the server: the app's database, and the last two backups.
cd ~/support-ai
docker compose exec -T api python -c "from app.config import settings; print(settings.database_url.rsplit('@', 1)[1])" </dev/null
tail -n 4 ~/backup.log

# On the Mac: the backups in S3, oldest first.
aws s3 ls s3://support-ai-docs-phanindra02/backups/
```

The first command prints `postgres:5432/support`. For the restore drill, follow "Restore a backup" in `infra/aws/README.md`: it brings the newest backup to the server, restores it into `restore_check`, compares the counts, and drops it.

## What an interviewer might ask

- **"Why move off a managed database?"** Cost against recovery. RDS was about $14 of the bill for 9 MB of data. The trade was an RPO of about 5 minutes for one of up to a day, and owning backups, restores, and minor updates. I wrote down what changed (`docs/system-design/18-backups-and-managed-databases.md`) before making the move.
- **"How would you get the RPO back down?"** Stream Postgres's write-ahead log to S3 with WAL-G or pgBackRest, for an RPO of about a minute. Or, more simply, dump every hour: at 114 KB a dump, that costs almost nothing.
- **"How do you know the backups work?"** A backup that's never been restored might not restore. The drill restores the newest one into a scratch database and compares every table's count and the migration. It took 0.64 seconds. The cron log and the list of files in S3 show the nightly runs.
- **"What happens if the server's disk dies?"** Build a new server from the AWS README, then restore the newest dump over its empty database. Up to a day of writes is lost. Recovery time is mostly building the server, since the restore takes under a second at this size.
- **"Could someone on the server destroy the backups?"** Not by deleting or listing them: the role can't. But `PutObject` can overwrite a key that already exists, and the names are times, so someone who guessed a name could replace that backup. Turning on bucket versioning, or S3 Object Lock, would keep the old copy.
- **"How do you update Postgres now?"** Minor versions: pull a newer `pgvector/pgvector:...-pg16` image and recreate the container, since the data is in the volume. A major version (16 to 17) needs a dump and restore, or `pg_upgrade`, because the data files' format changes.
- **"Why did the move use expand and contract?"** So each step could be undone. Merging the container changed nothing the app used. The switch was one `.env` line, and emptying it switched back. RDS was to stay a week before deletion. Each step was safe to ship alone.
- **"What about connections?"** Postgres in the container allows 100 (the default). The API and worker pools could ask for more than that if many API copies ran. PgBouncer, or smaller pools, would come first.
