# Kubernetes (Stage 8)

The API, worker, model service, and Redis run on Kubernetes from one Helm chart, `support-ai/`. Postgres and n8n stay outside the cluster: on minikube, they're the Mac's.

- **`support-ai/templates/`**: a Deployment for each service, Services for all but the worker (it takes jobs from Redis, so nothing calls it), a PersistentVolumeClaim for Redis's append-only file, an autoscaler for the API, the two ConfigMaps of settings, and the migrations as a Job.
- **`support-ai/values.yaml`**: only what differs between clusters, such as the image tag, where the model service finds its models, and the settings. The defaults are minikube's.
- **`secrets.env.example`**: the keys for the `support-ai-secrets` Secret. The chart doesn't make the Secret, so no key passes through Helm or this repo.

## Running it on minikube

Postgres runs on the Mac (`brew services start postgresql@16`), at `host.minikube.internal` from inside minikube. Compose's n8n is optional: without it, an urgent ticket's alert fails, is logged, and nothing else changes.

```bash
# 1. Start minikube with the Mac's saved models mounted at /models inside it, so the model
#    service needs no AWS keys. ml/models/ comes from: uv run python -m ml.save
minikube start --driver=docker --cpus=4 --memory=4g --mount --mount-string="$PWD/ml/models:/models"
minikube addons enable metrics-server     # the CPU numbers the autoscaler reads

# 2. Build the images on the Mac and copy them into minikube. minikube runs containerd, so
#    building through `minikube docker-env` doesn't work.
docker build -t support-ai-api -f infra/api.Dockerfile .
docker build -t support-ai-model -f infra/model.Dockerfile .
minikube image load support-ai-api && minikube image load support-ai-model

# 3. Make the Secret. Copy secrets.env.example to secrets.env (git ignores it) and fill it in
#    from .env, with host.minikube.internal in DATABASE_URL instead of localhost.
kubectl create secret generic support-ai-secrets --from-env-file=infra/k8s/secrets.env

# 4. Install the chart. --wait returns once every pod is ready.
helm install support-ai infra/k8s/support-ai --wait --timeout 5m
kubectl get pods
```

The worker may restart once at first: it can start before Redis accepts connections, and Kubernetes has no `depends_on`. It restarts it until Redis is up.

To call the API from the Mac: `kubectl port-forward service/api 8080:8000`, then http://localhost:8080/docs.

## Changing it

```bash
# After changing the chart or values, or loading new images. The migrations run first.
helm upgrade support-ai infra/k8s/support-ai --wait
# After loading a new image with the same tag, which helm upgrade can't see as a change.
kubectl rollout restart deployment api worker model
# A setting from the command line, without editing values.yaml.
helm upgrade support-ai infra/k8s/support-ai --wait --set-string modelSettings.PRIORITY_MODEL_VERSION=2
```

## Stopping it

```bash
helm uninstall support-ai          # everything the chart made. The Secret stays.
minikube stop                      # keeps the cluster for next time, or: minikube delete
```
