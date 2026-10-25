"""The Kubernetes manifests in infra/k8s/ (Stage 8), read as YAML.

Kubernetes accepts a Service whose selector matches no pods, or a port name no container has,
and the mistake only shows up as requests that go nowhere. These tests catch that first.
"""

import subprocess

import pytest
import yaml

from app.config import PROJECT_ROOT, Settings
from ml.service import Settings as ModelSettings

K8S_DIR = PROJECT_ROOT / "infra" / "k8s"


@pytest.fixture(scope="module")
def resources():
    """Every resource in infra/k8s/, by kind and then name."""
    found = {}
    for path in sorted(K8S_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            found.setdefault(doc["kind"], {})[doc["metadata"]["name"]] = doc
    return found


def pod_spec(resource):
    return resource["spec"]["template"]["spec"]


def container(resource):
    [only] = pod_spec(resource)["containers"]
    return only


def test_the_api_worker_model_and_redis_each_have_a_deployment(resources):
    assert set(resources["Deployment"]) == {"api", "worker", "model", "redis"}


def test_each_deployment_selects_its_own_pods(resources):
    for name, deployment in resources["Deployment"].items():
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert deployment["spec"]["selector"]["matchLabels"] == {"app": name}
        assert labels == {"app": name}


def test_each_service_reaches_a_named_port_on_its_deployments_pods(resources):
    for name, service in resources["Service"].items():
        deployment = resources["Deployment"][name]
        assert service["spec"]["selector"] == deployment["spec"]["template"]["metadata"]["labels"]
        port_names = {port["name"] for port in container(deployment)["ports"]}
        for port in service["spec"]["ports"]:
            assert port["targetPort"] in port_names


def test_only_the_worker_has_no_service(resources):
    # Nothing calls the worker. It takes jobs from Redis.
    assert set(resources["Service"]) == {"api", "model", "redis"}


def test_the_api_worker_and_migrations_share_the_api_image_and_settings(resources):
    users = [resources["Deployment"]["api"], resources["Deployment"]["worker"]]
    users.append(resources["Job"]["migrate"])
    for resource in users:
        assert container(resource)["image"] == "support-ai-api"
        assert container(resource)["envFrom"] == [
            {"configMapRef": {"name": "support-ai-settings"}},
            {"secretRef": {"name": "support-ai-secrets"}},
        ]


def test_images_built_on_the_mac_are_not_downloaded(resources):
    # On minikube, the images exist only where they were built.
    for resource in [*resources["Deployment"].values(), resources["Job"]["migrate"]]:
        if container(resource)["image"].startswith("support-ai-"):
            assert container(resource)["imagePullPolicy"] == "IfNotPresent"


def test_the_worker_runs_the_worker_and_gets_time_to_finish_a_job(resources):
    worker = resources["Deployment"]["worker"]
    assert container(worker)["command"] == ["python", "-m", "worker.main"]
    assert pod_spec(worker)["terminationGracePeriodSeconds"] >= 60


def test_the_model_service_gets_none_of_the_apis_secrets(resources):
    model = resources["Deployment"]["model"]
    assert container(model)["image"] == "support-ai-model"
    assert container(model)["envFrom"] == [{"configMapRef": {"name": "model-settings"}}]


def test_the_model_service_keeps_its_models_where_it_looks_for_them(resources):
    model = resources["Deployment"]["model"]
    [mount] = container(model)["volumeMounts"]
    # MODELS_DIR in infra/model.Dockerfile.
    assert mount["mountPath"] == "/models"
    assert mount["name"] in {volume["name"] for volume in pod_spec(model)["volumes"]}


def test_redis_keeps_its_data_on_a_disk_that_outlives_the_pod(resources):
    redis = resources["Deployment"]["redis"]
    assert redis["spec"]["replicas"] == 1
    # The disk attaches to one node at a time, so the old pod must stop first.
    assert redis["spec"]["strategy"] == {"type": "Recreate"}
    assert "--appendonly" in container(redis)["args"]
    [volume] = pod_spec(redis)["volumes"]
    assert volume["persistentVolumeClaim"]["claimName"] in resources["PersistentVolumeClaim"]
    assert container(redis)["volumeMounts"] == [{"name": volume["name"], "mountPath": "/data"}]


def test_the_migrations_run_once_to_the_latest(resources):
    migrate = resources["Job"]["migrate"]
    assert container(migrate)["command"] == ["alembic", "upgrade", "head"]
    assert pod_spec(migrate)["restartPolicy"] == "Never"


# Settings


SECRETS_EXAMPLE = K8S_DIR / "secrets.env.example"


def secret_keys():
    """The keys in secrets.env.example, the template for the support-ai-secrets Secret."""
    lines = SECRETS_EXAMPLE.read_text().splitlines()
    return {line.split("=")[0] for line in lines if line and not line.startswith("#")}


def test_every_configmap_and_secret_the_pods_ask_for_exists(resources):
    for resource in [*resources["Deployment"].values(), resources["Job"]["migrate"]]:
        for source in container(resource).get("envFrom", []):
            if "configMapRef" in source:
                assert source["configMapRef"]["name"] in resources["ConfigMap"]
            else:
                # Made from secrets.env, so it isn't a file here.
                assert source["secretRef"]["name"] == "support-ai-secrets"
    assert "secret generic support-ai-secrets" in SECRETS_EXAMPLE.read_text()


def test_every_setting_is_one_the_app_reads(resources):
    api_settings = {name.upper() for name in Settings.model_fields}
    model_settings = {name.upper() for name in ModelSettings.model_fields}
    assert set(resources["ConfigMap"]["support-ai-settings"]["data"]) <= api_settings
    assert secret_keys() <= api_settings
    assert set(resources["ConfigMap"]["model-settings"]["data"]) <= model_settings


def test_keys_are_in_the_secret_and_never_in_a_configmap(resources):
    for name in Settings.model_fields:
        if name.endswith(("_key", "_secret", "_token", "database_url")) and "test_" not in name:
            assert name.upper() in secret_keys()
    for configmap in resources["ConfigMap"].values():
        assert not set(configmap["data"]) & secret_keys()
        for value in configmap["data"].values():
            # A URL with a password in it, like DATABASE_URL's.
            assert "@" not in value


def test_the_urls_point_at_the_services_here(resources):
    data = resources["ConfigMap"]["support-ai-settings"]["data"]
    for url, service in [(data["REDIS_URL"], "redis"), (data["MODEL_SERVICE_URL"], "model")]:
        [port] = resources["Service"][service]["spec"]["ports"]
        assert f"://{service}:{port['port']}" in url


def test_configmap_values_are_strings(resources):
    # Kubernetes refuses a ConfigMap with a number in it.
    for configmap in resources["ConfigMap"].values():
        assert all(isinstance(value, str) for value in configmap["data"].values())


def test_the_filled_in_secrets_file_is_never_committed():
    # Exits 0 if git ignores the path, and 1 if not.
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "infra/k8s/secrets.env"], check=False, cwd=PROJECT_ROOT
    )
    assert ignored.returncode == 0
