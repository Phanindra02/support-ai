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

## The EKS cluster

`eks-cluster.yaml` is the cluster for eksctl: Kubernetes 1.37, two t4g.medium nodes, in the default VPC with RDS and the EC2 server. It bills by the hour, so it exists only for the demo.

| | Per hour |
|---|---|
| EKS control plane | $0.10 |
| 2 × t4g.medium | $0.067 |
| 2 nodes' public IPv4 addresses and 20 GB disks | $0.014 |
| **About** | **$0.18, or $4.30 a day** |

The load balancer for HTTPS adds about $0.03 an hour. The nodes sit in the VPC's public subnets, which saves a NAT gateway ($0.045 an hour).

### Creating it

About 20 minutes. eksctl also points kubectl at the new cluster: `kubectl config use-context minikube` goes back, and `kubectl config get-contexts` lists both.

```bash
eksctl create cluster -f infra/k8s/eks-cluster.yaml
kubectl get nodes
```

RDS's security group lets in only the EC2 server. Let in the cluster too. EKS gives every node the cluster's own security group, and the pods' traffic leaves from the nodes:

```bash
CLUSTER_SG=$(aws eks describe-cluster --region us-east-2 --name support-ai --query cluster.resourcesVpcConfig.clusterSecurityGroupId --output text)
aws ec2 authorize-security-group-ingress --region us-east-2 --group-id sg-05e1413ca647a380c --protocol tcp --port 5432 --source-group "$CLUSTER_SG"
```

### Giving the pods IAM roles, and installing the chart

The cluster file's `iam` section names two roles: one for the API, worker, and migrations (uploads, document text, and the keys in Parameter Store), and one for the model service (only its models). eksctl makes them, trusting only this cluster's pods, and links each to its service account:

```bash
eksctl create podidentityassociation -f infra/k8s/eks-cluster.yaml
```

Then install the chart with `values-eks.yaml`: CI's images from ECR, no Secret (the keys come from Parameter Store, as on the server), and the models from S3. The tag is the last commit on `main`, whose images CI pushed:

```bash
REGISTRY=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-2.amazonaws.com
TAG=sha-$(git rev-parse origin/main | cut -c1-7)
helm upgrade --install support-ai infra/k8s/support-ai -f infra/k8s/support-ai/values-eks.yaml \
  --set "image.api=$REGISTRY/support-ai-api,image.model=$REGISTRY/support-ai-model,image.tag=$TAG" \
  --wait --timeout 10m
```

Urgent alerts are skipped on EKS, with a warning in the worker's log: n8n stays on the EC2 server, out of the cluster's reach.

### HTTPS from the internet

`values-eks.yaml` turns on the chart's Ingress for `support.mysolospark.com`. The AWS Load Balancer Controller turns it into an Application Load Balancer: HTTPS on 443 with a free ACM certificate, and HTTP redirected to HTTPS. Three things come first, once per cluster:

1. **The controller's IAM role.** It's in `eks-cluster.yaml`, so a new cluster gets it with the others. For a cluster made before it was added:
   ```bash
   eksctl create podidentityassociation --cluster support-ai --region us-east-2 --namespace kube-system \
     --service-account-name aws-load-balancer-controller --role-name support-ai-eks-lb-controller \
     --well-known-policies=awsLoadBalancerController
   ```
2. **The certificate**, once per domain, in us-east-2 with the load balancer. ACM checks you own the domain through a CNAME record. The DNS is at Cloudflare, so the record goes there, as **DNS only** (grey cloud), and stays, because ACM renews through it.
   ```bash
   aws acm request-certificate --region us-east-2 --domain-name support.mysolospark.com --validation-method DNS
   aws acm describe-certificate --region us-east-2 --certificate-arn <the ARN> \
     --query 'Certificate.DomainValidationOptions[0].ResourceRecord'   # the CNAME to add
   ```
3. **The controller**, from AWS's Helm chart:
   ```bash
   helm repo add eks https://aws.github.io/eks-charts
   helm install aws-load-balancer-controller eks/aws-load-balancer-controller -n kube-system --version 3.6.2 \
     --set clusterName=support-ai --set region=us-east-2 --set vpcId=vpc-0f9a361a3857764b0 \
     --set serviceAccount.name=aws-load-balancer-controller --wait
   ```

After `helm upgrade` with `values-eks.yaml`, `kubectl get ingress api` shows the load balancer's address. To try it before moving the domain, send a request for the domain to that address: `curl --resolve support.mysolospark.com:443:$(dig +short <address> | head -1) https://support.mysolospark.com/health`.

Moving the domain means changing Cloudflare's `support` record from the EC2 server's IP to a CNAME for the load balancer's address, DNS only. Twilio's webhook URL stays the same. Moving back is the reverse.

### Deleting it

In this order. Point the domain back at the EC2 server first. Then the chart's resources, so the load balancer and Redis's EBS volume, which Kubernetes made in AWS, are deleted with them. Then RDS's rule, because a security group can't be deleted while another one's rule names it.

```bash
helm uninstall support-ai
CLUSTER_SG=$(aws eks describe-cluster --region us-east-2 --name support-ai --query cluster.resourcesVpcConfig.clusterSecurityGroupId --output text)
aws ec2 revoke-security-group-ingress --region us-east-2 --group-id sg-05e1413ca647a380c --protocol tcp --port 5432 --source-group "$CLUSTER_SG"
eksctl delete cluster -f infra/k8s/eks-cluster.yaml --wait
```
