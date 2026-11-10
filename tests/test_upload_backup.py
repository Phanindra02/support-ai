"""scripts/upload_backup.py uploads the nightly pg_dump to S3, under backups/."""

import io
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from app.config import settings
from scripts import upload_backup

BUCKET = "support-ai-backups-test"
TAKEN_AT = datetime(2026, 11, 11, 8, 0, 5, tzinfo=timezone.utc)
# A stand-in for pg_dump's output: the custom format's first bytes, then binary data.
DUMP = upload_backup.CUSTOM_FORMAT_HEADER + bytes(range(256)) * 40


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-2")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-east-2"}
        )
        yield client


@pytest.fixture
def script(s3, monkeypatch):
    """The script's main(), uploading to the fake S3's bucket."""
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(upload_backup, "get_s3_client", lambda: s3)
    return upload_backup.main


def saved_keys(s3):
    return [item["Key"] for item in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


def test_a_backup_is_saved_under_backups_and_named_by_when_it_was_taken(s3):
    key = upload_backup.upload_backup(s3, BUCKET, io.BytesIO(DUMP), TAKEN_AT)

    assert key == "backups/support-20261111T080005Z.dump"
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == DUMP


def test_backup_names_sort_oldest_first():
    # aws s3 ls and the restore steps list them by name, so name order must be time order.
    times = [
        datetime(2026, 12, 31, 8, tzinfo=timezone.utc),
        datetime(2027, 1, 1, 8, tzinfo=timezone.utc),
        datetime(2027, 1, 1, 9, 30, tzinfo=timezone.utc),
        datetime(2027, 1, 10, 8, tzinfo=timezone.utc),
    ]
    names = [upload_backup.backup_key(time) for time in times]
    assert sorted(names) == names
    assert all(name.startswith(upload_backup.BACKUP_PREFIX) for name in names)


@pytest.mark.parametrize(
    "not_a_dump",
    [
        b"",
        b"pg_dump: error: connection to server failed\n",
        # pg_dump's plain format, which pg_restore can't read.
        b"--\n-- PostgreSQL database dump\n--\n",
    ],
)
def test_anything_but_a_custom_format_dump_is_refused(s3, not_a_dump):
    with pytest.raises(ValueError, match="pg_dump --format=custom"):
        upload_backup.upload_backup(s3, BUCKET, io.BytesIO(not_a_dump), TAKEN_AT)

    assert saved_keys(s3) == []


def test_the_script_uploads_what_is_piped_in_and_says_where(script, s3, capsys):
    script(io.BytesIO(DUMP))

    [key] = saved_keys(s3)
    assert key.startswith(upload_backup.BACKUP_PREFIX)
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == DUMP
    assert capsys.readouterr().out == f"Saved s3://{BUCKET}/{key} ({len(DUMP):,} bytes)\n"


def test_the_script_fails_and_uploads_nothing_when_nothing_is_piped_in(script, s3):
    # Exiting with a message makes the exit status 1, so the cron log shows the failure.
    with pytest.raises(SystemExit, match="wasn't uploaded"):
        script(io.BytesIO(b""))

    assert saved_keys(s3) == []


def test_the_script_needs_a_bucket(monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket", None)

    with pytest.raises(SystemExit, match="S3_BUCKET"):
        upload_backup.main(io.BytesIO(DUMP))
