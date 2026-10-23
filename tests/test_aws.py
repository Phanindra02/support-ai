"""The AWS setup in infra/aws must keep the help docs private, and give CI no more than it needs.

These tests read the files the README applies with the AWS CLI. They never call AWS.
"""

import json
import re
import ssl

import botocore.session
import extract_text
import pytest
import yaml
from botocore.validate import ParamValidator
from sqlalchemy import make_url

from app.config import PROJECT_ROOT, Settings
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


def test_only_web_traffic_is_open_to_everyone():
    # Let's Encrypt checks the domain from addresses it doesn't publish, so Caddy's ports are
    # open to all (443 on UDP too, for HTTP/3). SSH is only ever let in from one address.
    readme = (AWS / "README.md").read_text()
    rules = re.findall(
        r"authorize-security-group-ingress[^\n]*\\\n\s*--protocol (\w+) --port (\d+) --cidr ([^\n]+)",
        readme,
    )
    open_to_all = {(protocol, port) for protocol, port, cidr in rules if cidr == "0.0.0.0/0"}
    assert open_to_all == {("tcp", "80"), ("tcp", "443"), ("udp", "443")}
    ssh = [cidr for protocol, port, cidr in rules if port == "22"]
    assert ssh
    assert all(cidr.endswith('/32"') for cidr in ssh)


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
    # Each whole command: anything but a line break, or a backslash that continues the line.
    commands = re.findall(r"aws ssm put-parameter(?:[^\n\\]|\\\n)*", readme)
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


# The Lambda that pulls the text out of uploads (lambda/extract_text.py).


@pytest.fixture(scope="module")
def function():
    return json.loads((AWS / "lambda-function.json").read_text())


@pytest.fixture(scope="module")
def lambda_permissions():
    statements = json.loads((AWS / "lambda-permissions.json").read_text())["Statement"]
    return {statement["Sid"]: statement for statement in statements}


@pytest.fixture(scope="module")
def trigger():
    notification = json.loads((AWS / "docs-bucket-notification.json").read_text())
    [trigger] = notification["LambdaFunctionConfigurations"]
    return trigger


def test_the_function_settings_are_ones_lambda_accepts(function):
    # The role and the zip are given on the command line, so stand-ins fill them here.
    request = {**function, "Role": "arn:aws:iam::ACCOUNT_ID:role/r", "Code": {"ZipFile": b""}}
    model = botocore.session.get_session().get_service_model("lambda")
    report = ParamValidator().validate(request, model.operation_model("CreateFunction").input_shape)
    assert not report.has_errors(), report.generate_report()


def test_the_function_runs_the_projects_python_on_graviton(function):
    python_version = (PROJECT_ROOT / ".python-version").read_text().strip()
    assert function["Runtime"] == f"python{python_version}"
    assert function["Architectures"] == ["arm64"]


def test_the_function_starts_at_the_handler(function):
    module, name = function["Handler"].split(".")
    assert (PROJECT_ROOT / "lambda" / f"{module}.py").exists()
    assert callable(getattr(extract_text, name))


def test_the_function_calls_the_api_at_caddys_domain(function):
    [site] = re.findall(r"^(\S+) \{$", (AWS / "Caddyfile").read_text(), re.MULTILINE)
    assert function["Environment"]["Variables"]["API_URL"] == f"https://{site}"


def test_the_function_and_the_api_read_the_same_secret(function):
    # The API loads everything directly under PARAMETER_STORE_PATH, and the parameter's name
    # is the setting it fills.
    parameter = function["Environment"]["Variables"]["SECRET_PARAMETER"]
    compose = yaml.load((AWS / "compose.ec2.yml").read_text(), Loader=IgnoreTagsLoader)
    path = compose["services"]["api"]["environment"]["PARAMETER_STORE_PATH"]
    folder, name = parameter.rsplit("/", 1)
    assert folder == path
    assert name.lower() == "lambda_webhook_secret" and name.lower() in Settings.model_fields


def test_the_function_has_time_to_wait_for_the_api(function):
    assert function["Timeout"] > extract_text.API_TIMEOUT_SECONDS


def test_only_lambda_can_use_the_functions_role():
    [statement] = json.loads((AWS / "lambda-trust-policy.json").read_text())["Statement"]
    assert statement["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"


def test_the_function_reads_uploads_and_writes_only_text(lambda_permissions):
    assert set(lambda_permissions) == {
        "ReadUploadedDocs",
        "SaveTheirText",
        "ReadTheWebhookSecret",
        "WriteLogs",
    }
    assert all(statement["Effect"] == "Allow" for statement in lambda_permissions.values())
    read = lambda_permissions["ReadUploadedDocs"]
    assert read["Action"] == "s3:GetObject"
    assert read["Resource"] == f"arn:aws:s3:::{BUCKET}/{UPLOAD_PREFIX}*"
    # It can add text, but never change or delete an upload.
    save = lambda_permissions["SaveTheirText"]
    assert save["Action"] == "s3:PutObject"
    assert save["Resource"] == f"arn:aws:s3:::{BUCKET}/{extract_text.TEXT_PREFIX}*"


def test_the_function_reads_only_its_own_secret(function, lambda_permissions):
    secret = lambda_permissions["ReadTheWebhookSecret"]
    assert secret["Action"] == "ssm:GetParameter"
    parameter = function["Environment"]["Variables"]["SECRET_PARAMETER"]
    assert secret["Resource"] == f"arn:aws:ssm:us-east-2:ACCOUNT_ID:parameter{parameter}"


def test_the_function_writes_only_its_own_logs(function, lambda_permissions, readme):
    # The log group is made in the README, with a retention period, so the role doesn't need
    # logs:CreateLogGroup, and the logs don't pile up forever.
    log_group = f"/aws/lambda/{function['FunctionName']}"
    logs = lambda_permissions["WriteLogs"]
    assert set(logs["Action"]) == {"logs:CreateLogStream", "logs:PutLogEvents"}
    assert logs["Resource"] == f"arn:aws:logs:us-east-2:ACCOUNT_ID:log-group:{log_group}:*"
    assert f"--log-group-name {log_group} --retention-in-days" in readme


def test_only_new_uploads_start_the_function(function, trigger):
    assert trigger["Events"] == ["s3:ObjectCreated:*"]
    assert trigger["LambdaFunctionArn"].endswith(f":function:{function['FunctionName']}")
    rules = {rule["Name"]: rule["Value"] for rule in trigger["Filter"]["Key"]["FilterRules"]}
    assert rules == {"prefix": UPLOAD_PREFIX, "suffix": ".pdf"}


def test_the_functions_own_text_cant_start_it_again(trigger):
    # The text is saved in the same bucket. If the trigger matched it, the function would run
    # on its own output, over and over.
    rules = {rule["Name"]: rule["Value"] for rule in trigger["Filter"]["Key"]["FilterRules"]}
    text_key = extract_text.text_key_for(UPLOAD_PREFIX + "0" * 32 + ".pdf")
    assert not (text_key.startswith(rules["prefix"]) and text_key.endswith(rules["suffix"]))


def test_only_this_bucket_in_this_account_can_start_the_function(readme):
    # Without --source-account, a bucket with the same name in another account could, if this
    # one were ever deleted and its name taken.
    # The whole command: anything but a line break, or a backslash that continues the line.
    [command] = re.findall(r"aws lambda add-permission(?:[^\n\\]|\\\n)*", readme)
    assert "--principal s3.amazonaws.com" in command
    assert f"--source-arn arn:aws:s3:::{BUCKET}" in command
    assert '--source-account "$ACCOUNT_ID"' in command
