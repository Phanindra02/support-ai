# Service discovery, health checks, autoscaling, and observability

On one server, docker compose put every container on one network and they found each other by name. On Kubernetes, pods come and go on any node with new IP addresses, so four questions need answers: how does one service find another, how does Kubernetes know a pod works, how does the API grow under load, and how does anyone see what's happening.

## Service discovery

A pod's IP address changes every time it's replaced, so nothing should hold one. A Kubernetes **Service** is a fixed name and virtual IP in front of every ready pod with a matching label. The cluster's DNS (CoreDNS) answers the name, and kube-proxy spreads connections across the pods behind it.

In this project the chart gives the resources fixed names, so the settings can name them (`infra/k8s/support-ai/values.yaml`):

- The worker reaches the model service at `http://model:8000` and Redis at `redis://redis:6379/0`.
- The worker has no Service at all. Nothing calls it: it asks Redis for its next job. That's why adding worker pods needs no load balancing.
- From outside, the ALB from the Ingress (`templates/ingress.yaml`) is the way in. It sends traffic straight to the API pods' IPs (`target-type: ip`), not through a node.
- RDS is outside the cluster, so it's found by its normal DNS name from Parameter Store.

The alternative is client-side discovery, where callers look up a registry such as Consul and choose a pod themselves. Kubernetes Services do the same job with no extra code.

## Health checks

Kubernetes has three probes, and each answers a different question:

- **Startup**: has the app finished starting? Until it passes, the other two wait. The model service has one with 60 tries 2 seconds apart, because it may download its models from S3 first, and a slow start shouldn't look like a hang.
- **Readiness**: should this pod get requests right now? A pod that fails it is taken out of the Service, and put back when it passes. Nothing is restarted.
- **Liveness**: is the pod stuck? Failing it 3 times in a row restarts the container.

The API's `/health` deliberately skips the database and Redis. If RDS had a blip and every API pod's liveness failed because of it, Kubernetes would restart all of them at once, and the restarts wouldn't fix RDS. A health check should only fail for problems a restart fixes. The worker answers no HTTP, so its liveness probe runs `python -m worker.health`, which checks that this pod's RQ heartbeat is still in Redis. It has no readiness probe, since no Service sends it anything.

The ALB has its own health check on `/health` as well, so it stops sending to a pod even before Kubernetes notices.

## Autoscaling

The HorizontalPodAutoscaler (`templates/api.yaml`) keeps 2 to 6 API pods, aiming for 70% of each pod's 250m CPU request. Every 15 seconds it reads CPU from metrics-server and works out `desired = current × (current CPU ÷ target)`. It adds pods quickly and waits 5 minutes of lower load before removing any, so a dip doesn't cause churn.

Two points worth knowing:

- Utilization is a share of the pod's **request**, so the HPA only works if requests are set, and set from real measurements. These came from the server's numbers (`docs/results.md`).
- The HPA adds pods, not nodes. With two t4g.medium nodes, 6 API pods plus everything else fit. Beyond that, something like Karpenter or the Cluster Autoscaler would have to add nodes.

The worker could scale on queue length instead of CPU, since a long queue is the real sign it's behind. That needs a custom metric (for example KEDA reading the Redis list), which this project doesn't need at its volume.

## Observability

Observability is being able to answer questions about the running system from what it emits. It has three parts.

**Logs** are what each process writes. Every service here logs to stdout, which is the container convention: Kubernetes keeps each container's output, and `kubectl logs deploy/api` reads it. It's lost when a node goes away, so a real setup ships logs somewhere central, such as CloudWatch Logs through Fluent Bit.

**Metrics** are numbers over time: request rate, error rate, latency percentiles, CPU, queue length. Today the cluster has only metrics-server, which keeps current CPU and memory for the HPA and `kubectl top`, and no history. A real setup adds Prometheus to collect metrics, Grafana to graph them, and alerts on the four golden signals: latency, traffic, errors, and saturation.

**Traces** follow one request across services. A WhatsApp message goes from Twilio to the API, onto Redis, to the worker, to the model service, and to n8n, and a trace would show how long each hop took. This project has none yet. Adding them means OpenTelemetry in each service, with the trace ID carried in the RQ job so the worker's spans join the API's.

What to look at first when something is wrong: the metrics show that something changed and when, the traces show which service, and the logs show why.
