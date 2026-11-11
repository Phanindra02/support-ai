"""Upload a database backup to S3. The nightly backup on EC2 runs it, after pg_dump.

infra/aws/backup-database.sh dumps the database to a file, then pipes the file in here:
  docker compose run --rm --no-deps -T api python -m scripts.upload_backup < support.dump

It runs in the API's image because the server has no AWS CLI. boto3 is already here, and it
uploads with the instance role, which may add backups but not read or delete them.
"""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import BinaryIO

from botocore.client import BaseClient

from app.config import settings
from app.storage import get_s3_client

# The server's role may only add objects under this prefix (infra/aws/ec2-permissions.json),
# and the bucket deletes them after 14 days (infra/aws/docs-bucket-lifecycle.json).
BACKUP_PREFIX = "backups/"

# pg_dump's custom format starts with these bytes, so anything else isn't a backup: an empty
# file, or an error message that was piped in by mistake.
CUSTOM_FORMAT_HEADER = b"PGDMP"


def backup_key(taken_at: datetime) -> str:
    """Named by the UTC time it was taken, so the names sort oldest first."""
    return f"{BACKUP_PREFIX}support-{taken_at:%Y%m%dT%H%M%SZ}.dump"


def upload_backup(s3: BaseClient, bucket: str, file: BinaryIO, taken_at: datetime) -> str:
    """Upload a pg_dump file to the bucket and return its key. Raises ValueError for anything
    that isn't one, and botocore's errors if S3 can't be reached or refuses it."""
    if file.read(len(CUSTOM_FORMAT_HEADER)) != CUSTOM_FORMAT_HEADER:
        raise ValueError("This isn't a pg_dump --format=custom file, so it wasn't uploaded")
    file.seek(0)
    key = backup_key(taken_at)
    # upload_fileobj sends a big file in parts, several at a time, and retries a failed part.
    s3.upload_fileobj(file, bucket, key)
    return key


def main(stdin: BinaryIO | None = None) -> None:
    if not settings.s3_bucket:
        sys.exit("S3_BUCKET isn't set, so there's no bucket to upload the backup to")
    # Piped input can only be read once, from the start. A copy in a temporary file can go
    # back to the start after the header check.
    with tempfile.TemporaryFile() as file:
        shutil.copyfileobj(stdin or sys.stdin.buffer, file)
        size = file.tell()
        file.seek(0)
        try:
            key = upload_backup(
                get_s3_client(), settings.s3_bucket, file, datetime.now(timezone.utc)
            )
        except ValueError as error:
            sys.exit(str(error))
    print(f"Saved s3://{settings.s3_bucket}/{key} ({size:,} bytes)")


if __name__ == "__main__":
    main()
