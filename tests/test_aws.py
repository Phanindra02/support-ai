"""The AWS setup in infra/aws must keep the help docs private, and give CI no more than it needs.

These tests read the files the README applies with the AWS CLI. They never call AWS.
"""

import json
import re
import ssl

import botocore.session
import pytest
import yaml
from botocore.validate import ParamValidator
from sqlalchemy import make_url

from app.config import PROJECT_ROOT
from app.storage import UPLOAD_PREFIX

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


# The EC2 server.


@pytest.fixture(scope="module")
def server_permissions():
    statements = json.loads((AWS / "ec2-permissions.json").read_text())["Statement"]
    return {statement["Sid"]: statement for statement in statements}


def test_only_ec2_can_use_the_servers_role():
    [statement] = json.loads((AWS / "ec2-trust-policy.json").read_text())["Statement"]
    assert statement["Principal"] == {"Service": "ec2.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"


def test_the_server_can_only_add_uploads_to_the_bucket(server_permissions):
    save = server_permissions["SaveUploadedDocs"]
    assert set(save["Action"]) == {"s3:PutObject", "s3:AbortMultipartUpload"}
    # The same prefix the app saves under, so a change to one can't break uploads silently.
    assert save["Resource"] == f"arn:aws:s3:::{BUCKET}/{UPLOAD_PREFIX}*"


def test_the_server_can_pull_the_api_image_but_never_push(server_permissions):
    assert set(server_permissions) == {
        "SaveUploadedDocs",
        "LogInToEcr",
        "PullTheApiImage",
        "LoadTheAppSecrets",
    }
    pull = server_permissions["PullTheApiImage"]
    assert set(pull["Action"]) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert pull["Resource"] == "arn:aws:ecr:us-east-2:ACCOUNT_ID:repository/support-ai-api"
    assert server_permissions["LogInToEcr"]["Action"] == "ecr:GetAuthorizationToken"


def test_the_server_needs_imdsv2_and_lets_containers_reach_it():
    # A hop limit of 1 would keep the API's container from getting the role's credentials.
    readme = (AWS / "README.md").read_text()
    assert "--metadata-options HttpTokens=required,HttpPutResponseHopLimit=2" in readme


def test_first_boot_stops_at_the_first_failing_command():
    script = (AWS / "ec2-user-data.sh").read_text()
    assert script.startswith("#!/bin/bash\n")
    assert "\nset -euo pipefail\n" in script


def test_first_boot_restarts_docker_after_installing_it():
    # Installed during the first boot, Docker is left stopped unless it's restarted.
    script = (AWS / "ec2-user-data.sh").read_text()
    install = script.index("apt-get install -y docker-ce")
    assert script.index("systemctl restart docker.socket docker.service") > install


# The database (RDS).


class IgnoreTagsLoader(yaml.SafeLoader):
    """safe_load, but compose's !reset tag reads as None instead of failing."""


IgnoreTagsLoader.add_constructor("!reset", lambda loader, node: None)


@pytest.fixture(scope="module")
def database():
    return json.loads((AWS / "rds-instance.json").read_text())


@pytest.fixture(scope="module")
def readme():
    return (AWS / "README.md").read_text()


def test_the_database_settings_are_ones_rds_accepts(database):
    # botocore holds the same description of the RDS API that the AWS CLI checks input against,
    # so a misspelled setting fails here instead of when the database is created.
    rds = botocore.session.get_session().get_service_model("rds")
    report = ParamValidator().validate(
        database, rds.operation_model("CreateDBInstance").input_shape
    )
    assert not report.has_errors(), report.generate_report()


def test_the_database_is_the_smallest_graviton_instance_in_one_zone(database):
    assert database["DBInstanceClass"] == "db.t4g.micro"
    assert database["MultiAZ"] is False
    assert database["AvailabilityZone"].startswith("us-east-2")
    # 20 GB is the smallest gp3 size. Without MaxAllocatedStorage, storage never grows by itself.
    assert (database["StorageType"], database["AllocatedStorage"]) == ("gp3", 20)
    assert "MaxAllocatedStorage" not in database


def test_the_database_runs_the_same_postgres_as_compose_and_ci(database):
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    ci = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert database["Engine"] == "postgres"
    major = database["EngineVersion"].split(".")[0]
    assert compose["services"]["postgres"]["image"] == f"postgres:{major}"
    assert ci["jobs"]["test"]["services"]["postgres"]["image"] == f"postgres:{major}"


def test_the_database_has_no_public_address(database):
    assert database["PubliclyAccessible"] is False


def test_only_the_servers_security_group_can_reach_the_database(readme):
    # Allowed by security group, not by IP address, and never from a CIDR range like 0.0.0.0/0.
    [rule] = re.findall(
        r"authorize-security-group-ingress[^\n]*\\\n[^\n]*--port 5432[^\n]*", readme
    )
    assert rule.endswith('--source-group "$SERVER_SG_ID"')
    assert "--cidr" not in rule
    assert '--vpc-security-group-ids "$DB_SG_ID"' in readme


def test_the_database_is_encrypted_backed_up_and_hard_to_delete(database):
    assert database["StorageEncrypted"] is True
    assert database["BackupRetentionPeriod"] >= 7
    assert database["DeletionProtection"] is True
    assert database["CopyTagsToSnapshot"] is True


def test_the_database_never_moves_to_paid_extended_support(database):
    assert database["EngineLifecycleSupport"] == "open-source-rds-extended-support-disabled"


def test_no_database_password_is_written_in_the_file(database):
    # The README makes one when it creates the database. A managed password would rotate weekly
    # and break the URL stored in Parameter Store.
    assert "MasterUserPassword" not in database
    assert "ManageMasterUserPassword" not in database


def test_the_database_url_is_stored_encrypted_and_checks_the_certificate(database, readme):
    commands = re.findall(r"aws ssm put-parameter[^\n]*\\\n[^\n]*", readme)
    [command] = [command for command in commands if "/support-ai/DATABASE_URL" in command]
    assert "--name /support-ai/DATABASE_URL --type SecureString" in command
    [url] = re.findall(r'--value "([^"]+)"', command)
    url = make_url(url)
    assert url.drivername == "postgresql+psycopg"
    assert url.username == database["MasterUsername"]
    assert url.database == database["DBName"]
    # verify-full checks that the certificate is Amazon's and names this database's host.
    assert url.query == {"sslmode": "verify-full"}


def test_the_server_can_only_read_the_apps_own_parameters(server_permissions):
    # GetParametersByPath is checked against the path asked for, not each parameter returned,
    # so the resource is the path. Read only: no PutParameter or DeleteParameter.
    load = server_permissions["LoadTheAppSecrets"]
    assert load["Action"] == "ssm:GetParametersByPath"
    compose = yaml.load((AWS / "compose.ec2.yml").read_text(), Loader=IgnoreTagsLoader)
    path = compose["services"]["api"]["environment"]["PARAMETER_STORE_PATH"]
    assert load["Resource"] == f"arn:aws:ssm:us-east-2:ACCOUNT_ID:parameter{path}"


def test_every_secret_is_stored_encrypted(readme):
    # A String parameter would be readable by anyone allowed to list parameters.
    commands = re.findall(r"aws ssm put-parameter[^\n]*(?:\\\n[^\n]*)*", readme)
    assert commands
    for command in commands:
        assert "--type SecureString" in command, command


def test_the_certificates_are_amazons_roots_for_rds_in_us_east_2():
    # The ssl module reads the file the same way the database driver will, so a broken or
    # wrong file fails here.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(AWS / "rds-ca-us-east-2.pem")
    names = {
        dict(field[0] for field in cert["subject"])["commonName"] for cert in context.get_ca_certs()
    }
    assert names == {
        "Amazon RDS us-east-2 Root CA RSA2048 G1",
        "Amazon RDS us-east-2 Root CA RSA4096 G1",
        "Amazon RDS us-east-2 Root CA ECC384 G1",
    }
