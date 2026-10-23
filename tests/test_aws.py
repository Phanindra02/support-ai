"""The AWS setup in infra/aws must keep the help docs private.

These tests read the files the README applies with the AWS CLI. They never call AWS.
"""

import json

import pytest

from app.config import PROJECT_ROOT

AWS = PROJECT_ROOT / "infra" / "aws"
BUCKET = "support-ai-docs-phanindra02"


@pytest.fixture(scope="module")
def bucket_policy():
    return json.loads((AWS / "docs-bucket-policy.json").read_text())


def test_every_public_access_block_is_on():
    settings = json.loads((AWS / "docs-bucket-public-access-block.json").read_text())
    assert settings == {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }


def test_the_bucket_policy_only_denies(bucket_policy):
    # An Allow here could open the bucket to everyone. Access comes from IAM roles instead.
    assert {statement["Effect"] for statement in bucket_policy["Statement"]} == {"Deny"}


def test_plain_http_is_refused_for_the_bucket_and_every_object(bucket_policy):
    [statement] = [s for s in bucket_policy["Statement"] if s["Sid"] == "DenyPlainHttp"]
    assert statement["Principal"] == "*"
    assert statement["Action"] == "s3:*"
    assert set(statement["Resource"]) == {f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"}
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_the_readme_applies_both_files_to_the_same_bucket():
    readme = (AWS / "README.md").read_text()
    assert f"--bucket {BUCKET}" in readme
    assert "file://infra/aws/docs-bucket-public-access-block.json" in readme
    assert "file://infra/aws/docs-bucket-policy.json" in readme
