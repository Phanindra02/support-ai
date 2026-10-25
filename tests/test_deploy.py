"""CI deploys each merge to EC2 through AWS Systems Manager, and can do nothing else there.

These tests read the files. The first real deploy was checked by hand (infra/aws/README.md).
"""

import json
import os
import re
import subprocess

import pytest
import yaml

from app.config import PROJECT_ROOT

AWS = PROJECT_ROOT / "infra" / "aws"


@pytest.fixture(scope="module")
def document():
    return json.loads((AWS / "deploy-document.json").read_text())


@pytest.fixture(scope="module")
def deploy_job():
    return yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"][
        "deploy"
    ]


def statements(name):
    return {s["Sid"]: s for s in json.loads((AWS / name).read_text())["Statement"]}


def test_only_a_full_commit_id_can_be_passed_in(document):
    # SSM pastes the value into the script, so anything but hex digits could run as root.
    [(name, parameter)] = document["parameters"].items()
    assert (name, parameter["allowedPattern"]) == ("commit", "^[0-9a-f]{40}$")
    script = "\n".join(document["mainSteps"][0]["inputs"]["runCommand"])
    assert script.count("{{ commit }}") == 1 and "COMMIT={{ commit }}" in script


def test_the_deploy_runs_the_image_built_from_that_commit(document):
    # metadata-action's type=sha tag is sha- and the commit's first 7 characters.
    script = "\n".join(document["mainSteps"][0]["inputs"]["runCommand"])
    assert script.startswith("set -eu\n")
    assert "git merge --quiet --ff-only" in script
    assert ":sha-$SHORT" in script and "%.7s" in script
    assert "docker compose up -d --no-build" in script
    # Every command runs as ubuntu, who owns the repo and has the ECR login, not as root.
    commands = [c for c in script.splitlines() if re.match(r"(git|docker|sed) ", c)]
    assert commands == []


def test_the_deploy_points_both_images_at_that_commit(document, tmp_path):
    # Runs the document's own sed line on a made-up .env, printing instead of editing in place,
    # since the Mac's sed takes -i differently. The first version split its parts with "|",
    # which "(API|MODEL)" also holds, so sed refused it and no deploy ran after Stage 7.
    [line] = [c for c in document["mainSteps"][0]["inputs"]["runCommand"] if " sed " in c]
    command = line.removeprefix("as_ubuntu ").replace("sed -i -E", "sed -E")
    registry = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
    (tmp_path / ".env").write_text(
        f"API_IMAGE={registry}/support-ai-api:latest\n"
        f"MODEL_IMAGE={registry}/support-ai-model:sha-0000000\n"
        "S3_BUCKET=support-ai-docs\n"
    )

    result = subprocess.run(
        ["sh", "-c", command],
        # Not check=True: the assert below shows sed's own error instead.
        check=False,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "SHORT": "abc1234"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"API_IMAGE={registry}/support-ai-api:sha-abc1234\n"
        f"MODEL_IMAGE={registry}/support-ai-model:sha-abc1234\n"
        "S3_BUCKET=support-ai-docs\n"
    )


def test_ci_can_run_only_the_deploy_document_on_only_the_server():
    run = statements("github-ci-permissions.json")["RunTheDeployOnTheServer"]
    assert run["Action"] == "ssm:SendCommand"
    # Never AWS-RunShellScript, which would let CI run any command as root.
    assert set(run["Resource"]) == {
        "arn:aws:ssm:us-east-2:ACCOUNT_ID:document/support-ai-deploy",
        "arn:aws:ec2:us-east-2:ACCOUNT_ID:instance/INSTANCE_ID",
    }


def test_the_server_gets_only_what_the_ssm_agent_needs():
    talk = statements("ec2-permissions.json")["TalkToSystemsManager"]
    assert set(talk["Action"]) == {
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
    }


def test_deploys_run_after_the_image_on_main_one_at_a_time(deploy_job):
    assert deploy_job["needs"] == "image"
    assert deploy_job["if"] == "github.ref == 'refs/heads/main'"
    assert deploy_job["concurrency"] == {"group": "deploy", "cancel-in-progress": False}
    assert deploy_job["env"]["INSTANCE_ID"] == "${{ vars.SERVER_INSTANCE_ID }}"


def test_a_failed_deploy_fails_the_run_and_the_live_site_is_checked(deploy_job):
    script = "\n".join(step.get("run", "") for step in deploy_job["steps"])
    assert "--document-name support-ai-deploy" in script
    assert "commit=$GITHUB_SHA" in script
    assert 'test "$status" = Success' in script
    assert "curl --fail" in script and "https://support.mysolospark.com/" in script


def test_the_readme_creates_the_same_document():
    readme = (AWS / "README.md").read_text()
    assert "--name support-ai-deploy --document-type Command" in readme
    assert "file://infra/aws/deploy-document.json" in readme
    assert "gh variable set SERVER_INSTANCE_ID" in readme


@pytest.fixture(scope="module")
def eks_job():
    workflow = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    return workflow["jobs"]["deploy-eks"]


def test_merges_deploy_to_eks_only_while_the_cluster_exists(eks_job):
    assert eks_job["needs"] == "image"
    # EKS_CLUSTER is set only while the cluster is up, so after it's deleted the job is skipped.
    assert eks_job["if"] == "github.ref == 'refs/heads/main' && vars.EKS_CLUSTER != ''"
    assert eks_job["concurrency"] == {"group": "deploy-eks", "cancel-in-progress": False}
    assert eks_job["permissions"] == {"contents": "read", "id-token": "write"}


def test_the_eks_deploy_runs_this_commits_images_and_waits_for_them(eks_job):
    script = "\n".join(step.get("run", "") for step in eks_job["steps"])
    assert 'aws eks update-kubeconfig --region us-east-2 --name "$CLUSTER"' in script
    assert "helm upgrade --install support-ai infra/k8s/support-ai" in script
    assert "-f infra/k8s/support-ai/values-eks.yaml" in script
    assert "image.tag=sha-${GITHUB_SHA::7}" in script
    assert "--wait" in script


def test_ci_may_look_up_only_the_support_ai_cluster():
    find = statements("github-ci-permissions.json")["FindTheEksCluster"]
    assert find["Action"] == "eks:DescribeCluster"
    assert find["Resource"] == "arn:aws:eks:us-east-2:ACCOUNT_ID:cluster/support-ai"


def test_the_readme_lets_ci_change_only_the_default_namespace():
    readme = (PROJECT_ROOT / "infra" / "k8s" / "README.md").read_text()
    assert "aws eks create-access-entry" in readme
    assert "cluster-access-policy/AmazonEKSEditPolicy" in readme
    assert "--access-scope type=namespace,namespaces=default" in readme
    # Deleting the cluster starts by turning the CI deploy off.
    deleting = readme.split("### Deleting it")[1]
    assert deleting.index("gh variable delete EKS_CLUSTER") < deleting.index("eksctl delete")
