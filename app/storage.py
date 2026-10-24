"""Help docs in S3: the PDFs agents upload go to the private bucket in infra/aws/.

boto3 finds AWS credentials by itself, from environment variables, ~/.aws (on the Mac, after
aws configure), or the EC2 instance's IAM role, so no AWS keys are settings here.
"""

from functools import cache
from typing import BinaryIO
from uuid import uuid4

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from app.config import settings

# Every uploaded PDF is under this prefix. The Lambda that pulls out the text is triggered
# only by keys here, so its own output, saved elsewhere in the bucket, can't trigger it again.
UPLOAD_PREFIX = "uploads/"

# boto3 waits up to 60 seconds to connect and 60 to read, and tries 5 times. An upload holds
# up an API worker while it waits, so a slow or unreachable S3 fails sooner here.
CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 3},
)


@cache
def get_s3_client() -> BaseClient:
    """The S3 client, as a FastAPI dependency, so tests can swap in a fake.

    Made on first use rather than at import, because making one looks for credentials, and
    where there are none (as in CI) that can take a few seconds.
    """
    return boto3.client("s3", region_name=settings.aws_region, config=CONFIG)


def new_upload_key() -> str:
    """A key no other upload has, so no file can overwrite another."""
    return f"{UPLOAD_PREFIX}{uuid4().hex}.pdf"


def save_pdf(client: BaseClient, bucket: str, key: str, file: BinaryIO) -> None:
    """Upload a PDF to S3. Raises botocore's errors if S3 can't be reached or refuses it."""
    # upload_fileobj sends a big file in parts, several at a time, and retries a failed part.
    client.upload_fileobj(file, bucket, key, ExtraArgs={"ContentType": "application/pdf"})


def read_text(client: BaseClient, bucket: str, key: str) -> str:
    """A UTF-8 text file from S3, such as the text the Lambda pulled out of a PDF."""
    return client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
