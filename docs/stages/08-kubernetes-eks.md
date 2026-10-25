# Stage 8: Kubernetes and EKS

## What was built

- **Kubernetes manifests, packaged as a Helm chart** (`infra/k8s/support-ai/`): Deployments and Services for the API, worker, model service, and Redis; ConfigMaps for settings; the migrations as a Helm hook that runs before an upgrade replaces any pod; Redis's append-only file on a PersistentVolumeClaim. n8n stays on the EC2 server.
- **Probes, limits, and autoscaling:** readiness and liveness probes on every service, a startup probe on the model service (it may download models from S3 first), CPU and memory requests sized from the server's measurements, and a HorizontalPodAutoscaler keeping 2 to 6 API pods near 70% of their 250m CPU request.
- **minikube first**, with the Mac's Postgres and `ml/models/` mounted in, then **EKS** (`infra/k8s/eks-cluster.yaml`): Kubernetes 1.37 on two t4g.medium Graviton nodes, in the default VPC's public subnets beside RDS, so no NAT gateway.
- **IAM for pods through EKS Pod Identity:** one role for the API, worker, and migrations, and one for the model service that can only read `models/*`. The apps read their keys from Parameter Store, so the cluster holds no AWS keys and no Kubernetes Secret.
- **HTTPS Ingress:** the AWS Load Balancer Controller turns the chart's Ingress into an ALB, with a free ACM certificate validated through Cloudflare, and HTTP redirected to HTTPS.
- **CI deploys to EKS** (`deploy-eks` in `ci.yml`): `helm upgrade` with each merge's images, only while the `EKS_CLUSTER` variable is set.
- **A Locust load test** (`scripts/locustfile.py`) that took the API from 2 pods to 6 in about a minute.
- **Timeouts and retries** on the worker's calls to the model service: a 1-second connect timeout, and up to 3 attempts with jittered backoff for failures that might pass.
- Along the way: the EC2 server was rebuilt after being terminated by mistake, and the deploy document's `sed`, broken since Stage 7, was fixed with a test that runs it.

## Key decisions, and why

- **Helm over raw manifests.** One chart serves minikube and EKS, with `values-eks.yaml` holding the differences: images, the Ingress, no Secret. `helm upgrade` is also one command for CI.
- **RDS stays the database.** Postgres in the cluster would mean managing its disk, backups, and failover. RDS already does that, and the server uses it too.
- **Public subnets, no NAT gateway.** A NAT gateway costs about $32 a month on its own. Nodes in public subnets reach ECR, S3, and Parameter Store directly, and their security group still lets nothing in from outside except through the ALB.
- **Pod Identity, not IRSA or node roles.** A node role would give every pod the same permissions. Pod Identity gives each service account its own role, with no OIDC provider to set up, which is EKS's newer, simpler way.
- **The ALB sends traffic straight to pod IPs** (`target-type: ip`), and checks `/health` itself, so a pod leaves the load balancer as soon as it's unhealthy.
- **`/health` checks nothing outside the pod.** If RDS had a blip and every liveness probe checked it, Kubernetes would restart every API pod at once, and the restarts wouldn't fix RDS.
- **The CI deploy is gated on a variable.** The cluster is deleted after the demo. Without the gate, every later merge would fail its deploy.
- **CI gets the least it needs:** `eks:DescribeCluster` on this cluster only, and an access entry with `AmazonEKSEditPolicy` scoped to the `default` namespace, so it can't touch `kube-system` or anyone's permissions.
- **No circuit breaker yet.** Only the worker calls the model service, in the background, and RQ's own growing waits already back off. A breaker matters where a person waits (`docs/system-design/17-timeouts-retries-circuit-breakers.md`).

## How to run and demo it

minikube, on the Mac: `infra/k8s/README.md`, "Running it on minikube".

EKS (about $0.20 an hour while it exists): `infra/k8s/README.md`, from "Creating it" through "Deploying from CI". For the demo:

1. `kubectl get nodes,pods,hpa,ingress`: two Graviton nodes, every service ready, the HPA at 2 pods, and the ALB's address.
2. `https://support.mysolospark.com/health` and `/docs`, served by the cluster. Send a WhatsApp message and watch it in `kubectl logs -l app=api -f`.
3. Merge a small PR and watch the `deploy-eks` job run `helm upgrade`, then `helm history support-ai`.
4. The load test (`docs/results.md` has the setup) with `kubectl get hpa api -w` beside it, to show the pods going from 2 to 6.
5. Then delete it all, in the order in "Deleting it", so billing stops.

## What an interviewer might ask

- **What's the difference between a Deployment, a Service, and an Ingress?** A Deployment keeps N copies of a pod running and replaces them in a rolling update. A Service is a fixed name and virtual IP in front of the ready pods with a label. An Ingress is the rule for HTTP from outside, which a controller, here AWS's, turns into a real load balancer.
- **Readiness versus liveness?** Readiness decides whether a pod gets traffic. Liveness decides whether it gets restarted. Mixing them up restarts pods that are only busy.
- **How does the HPA decide?** `desired = current × (current CPU ÷ target)`, from metrics-server, every 15 seconds. It scales down only after 5 minutes of lower load. CPU is measured against the request, so requests have to be set from real numbers.
- **Why did the load test stop at 6 pods?** That's `maxReplicas`. CPU stayed at 74-78%, just over the target, so 6 was about right for 182 requests a second. More would need a higher maximum, and then more nodes, which the HPA can't add. Karpenter or the Cluster Autoscaler would.
- **How do pods get AWS permissions without keys?** EKS Pod Identity: an agent on each node hands the pod short-lived credentials for the role linked to its service account.
- **How does a deploy avoid downtime?** The rolling update starts new pods, waits for their readiness probes, then stops old ones. The migrations run first, as a Helm hook, and only additive migrations are safe while old pods still run.
- **What went wrong, and what did you learn?** Redis couldn't write to its new EBS disk until `fsGroup: 999` gave its user the folder. A `sed` line that used `|` as its separator, in a pattern holding `|`, broke every deploy for a stage, so now a test runs it. And the always-on server was terminated by mistake. Termination protection is back on, and stopping, not terminating, is the habit for saving money.
