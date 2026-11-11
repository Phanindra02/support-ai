# Backups and managed databases

What Stage 10 gave up, and kept, by moving Postgres from RDS back onto the server.

## RPO and RTO

Two numbers describe any recovery plan:

- **RPO (recovery point objective):** how much recent data can be lost, measured in time. Backups once a night mean up to a day.
- **RTO (recovery time objective):** how long until the system works again after a failure.

| | RDS (Stages 5 to 9) | The container and a nightly dump (Stage 10) |
| --- | --- | --- |
| **RPO** | About 5 minutes. RDS restores to any moment of the last 7 days. | Up to 24 hours, if the server's disk is lost. |
| **RTO** | Restoring makes a new instance, about as slow as creating one (about 10 minutes), then the URL changes. | A new server from `infra/aws/README.md`, then the newest dump restored. The restore drill measures the restore part: 0.64 seconds for today's few megabytes, plus 1.8 to bring the file over (`docs/results.md`). |
| **Where the copies are** | RDS's backup storage, apart from the instance. | S3, apart from the server, for 14 days. |
| **Cost** | About $14 a month for the instance. | Well under a cent a month for the dumps. |

## Full dumps and point-in-time recovery

- **A full dump** (`pg_dump`) writes the whole database as one file. It's simple, it restores into any Postgres of the same or a newer major version, and it reads a consistent snapshot while the app keeps writing. But a restore only gets back to the moment it was taken, and on a database of hundreds of gigabytes it takes hours.
- **Point-in-time recovery** keeps a copy of the data files plus every change Postgres writes to its write-ahead log (WAL), and replays the log up to any chosen moment. RDS does this for you. Without RDS, tools like WAL-G or pgBackRest stream the WAL to S3. RPO drops to about a minute, at the cost of one more thing to run and watch.
- **Here:** the database is a few megabytes, so a dump takes seconds, and losing a day of a portfolio project's tickets is acceptable. A business whose customers' messages mattered would run WAL-G, or stay on RDS.

## Managed or self-run

RDS did these by itself: nightly backups with point-in-time recovery, Postgres's minor fixes, storage on its own disks, and a restart if Postgres crashed. On the server, Docker's restart policy still restarts it, but the backups are a cron job, the fixes arrive when a newer image is pulled, and the data shares a disk with everything else.

Running it yourself saves money and keeps one setup everywhere: the server now runs the same `pgvector/pgvector` image as the Mac's compose and CI. Pick managed when the data is worth more than the bill, or when nobody wants to be paged for the database. Pick self-run when the budget is tight, the data is small, and a day's loss is survivable.

## A backup isn't done until it restores

A backup that has never been restored is only a hope. The README's restore drill puts the newest dump into a scratch database and counts the rows, which tests the file, the format, and the steps without touching the real data. The uploader also refuses anything that doesn't start the way a `pg_dump` custom-format file does, so a broken run fails in the log instead of saving an empty backup.

## Moving a database safely

The move follows expand, switch, contract:

1. **Expand:** run the new database next to the old one. Merging it changes nothing the app uses.
2. **Switch:** copy the data with the writers stopped, check the row counts, and move the app with one line in `.env`. Emptying the line moves it back.
3. **Contract:** after a week of clean nightly backups, delete RDS, then remove the switch from the code.

Each step can be undone until the last one, and the last happens only after the new setup has proven itself.

## In an interview

"Why leave RDS?" It was about $14 of a $19 bill, for a few megabytes of data. The trade was an RPO of 5 minutes for one of a day, and owning backups and patches. "How would you bring the RPO back down?" Stream the WAL to S3 with WAL-G, for about a minute, or dump hourly, which costs little at this size. "How do you know the backups work?" The cron log, the files in S3, and the restore drill. "What happens if the server's disk dies?" A new server from the README, and the newest dump restored: up to a day lost. "Isn't the database's password on the server's disk now?" Yes, in `.env`. But it opens only a database with no published port, whose data is on that same disk, so it gives nothing that access to the server doesn't already give.
