"""The AWS setup in infra/aws must keep the help docs private, and give CI no more than it needs.

These tests read the files the README applies with the AWS CLI. They never call AWS.
"""

import json
import re

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


# The registry, and the role CI uses to push to it.

# What GitHub's token says for a push to main. The repo uses immutable subjects, which carry
# the owner's and the repo's ids. infra/aws/README.md shows how to look them up.
MAIN_BRANCH_SUBJECT = "repo:Phanindra02@60551624/support-ai@1424736820:ref:refs/heads/main"


@pytest.fixture(scope="module")
def trust_policy():
    return json.loads((AWS / "github-ci-trust-policy.json").read_text())


@pytest.fixture(scope="module")
def ci_permissions():
    return json.loads((AWS / "github-ci-permissions.json").read_text())


def test_ecr_keeps_the_ten_newest_builds_and_drops_untagged_images():
    rules = json.loads((AWS / "ecr-lifecycle-policy.json").read_text())["rules"]
    selections = {rule["selection"]["tagStatus"]: rule["selection"] for rule in rules}
    assert selections["tagged"]["tagPrefixList"] == ["sha-"]
    assert selections["tagged"]["countType"] == "imageCountMoreThan"
    assert selections["tagged"]["countNumber"] == 10
    assert selections["untagged"]["countType"] == "sinceImagePushed"
    assert all(rule["action"] == {"type": "expire"} for rule in rules)


def test_only_github_actions_tokens_can_use_the_ci_role(trust_policy):
    [statement] = trust_policy["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Principal"] == {
        "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    }


def test_only_pushes_to_main_of_this_repo_can_use_the_ci_role(trust_policy):
    [statement] = trust_policy["Statement"]
    # StringEquals only. A StringLike with a * could let other branches or repos in.
    assert statement["Condition"] == {
        "StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": MAIN_BRANCH_SUBJECT,
        }
    }


def test_the_ci_role_can_only_log_in_to_ecr_and_push_one_image(ci_permissions):
    statements = {statement["Sid"]: statement for statement in ci_permissions["Statement"]}
    assert set(statements) == {"LogInToEcr", "PushTheApiImage"}
    assert all(statement["Effect"] == "Allow" for statement in statements.values())
    # GetAuthorizationToken can't be limited to a repository, so it's the only "*".
    assert statements["LogInToEcr"]["Action"] == "ecr:GetAuthorizationToken"
    assert statements["LogInToEcr"]["Resource"] == "*"
    push = statements["PushTheApiImage"]
    assert set(push["Action"]) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }
    assert push["Resource"] == "arn:aws:ecr:us-east-2:ACCOUNT_ID:repository/support-ai-api"


def test_no_aws_account_number_is_committed():
    # The README fills in ACCOUNT_ID when it applies the files, so the public repo never shows it.
    for path in AWS.iterdir():
        assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", path.read_text()), path


def test_the_readme_commands_work_in_zsh():
    # zsh reads $ACCOUNT_ID:r as "ACCOUNT_ID without its extension", so
    # "arn:aws:iam::$ACCOUNT_ID:role/..." loses the ":r". ${ACCOUNT_ID} is safe in every shell.
    assert "$ACCOUNT_ID:" not in (AWS / "README.md").read_text()
