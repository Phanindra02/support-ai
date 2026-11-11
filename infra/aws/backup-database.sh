#!/bin/bash
# Backs up the server's Postgres to the docs bucket, under backups/. The ubuntu user's crontab
# runs it every night (infra/aws/README.md, "Back up the database").
#
# pg_dump writes to a file, and the upload starts only once it has finished. Piped straight
# into the upload, a pg_dump that failed halfway would still have uploaded half a backup.
set -euo pipefail

echo "Backup started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Where the server cloned the repo, so compose finds docker-compose.yml and .env.
cd /home/ubuntu/support-ai

# mktemp makes a file only its owner can read, and the trap deletes it however the script ends.
FILE=$(mktemp)
trap 'rm -f "$FILE"' EXIT

# A consistent snapshot, taken while the API keeps running. The custom format is compressed,
# and pg_restore can restore all of it or single tables.
docker compose exec -T postgres pg_dump -U support -d support --format=custom > "$FILE"
docker compose run --rm --no-deps -T api python -m scripts.upload_backup < "$FILE"
