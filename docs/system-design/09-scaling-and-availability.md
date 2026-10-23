# Scaling and availability

How a service grows past one machine, and keeps running when a machine or a data center fails, as it applies to this project.

## Vertical vs horizontal scaling

- **Vertical:** a bigger machine. The server is a `t4g.small` (2 vCPUs, 2 GB). Going to a `t4g.large` is a stop, a change of instance type, and a start, a few minutes of downtime and no code change. It's the simplest step, but there's a largest machine, and one machine is still one machine.
- **Horizontal:** more machines behind a load balancer. That needs every copy of the API to be interchangeable, which is what "stateless" means below. Stage 8 does this with Kubernetes pods and an autoscaler.
- **This project** scales vertically on EC2 because its traffic is tiny, and is built so it can scale horizontally later.

## Load balancers

A load balancer spreads requests over several servers, checks their health, and stops sending to one that fails. Layer 4 balancers (AWS's NLB) pass TCP connections along. Layer 7 (AWS's ALB) read HTTP, so they can route by path and end TLS. Caddy here is a reverse proxy in front of one server: it ends TLS and forwards to the API, but has nothing to balance. In Stage 8, an ALB balances across API pods.

## Stateless services

A stateless API keeps nothing between requests in its own memory or disk, so any copy can answer any request, and a copy can be replaced at any time.

- **Already stateless:** logins are JWTs, which any copy can check with the shared key, rather than sessions in one process's memory. The rate-limit counters and the cache are in Redis, shared by every copy. Uploads go straight to S3, not the server's disk. The database is RDS.
- **Still on the one server:** Redis and n8n run as containers there. A second API server would need Redis moved out (ElastiCache), or each server would count its own rate limit.

## Availability zones

A region (`us-east-2`) has several availability zones: separate data centers with their own power and network, close enough for fast links. Spreading copies across zones survives the loss of one.

- The server and RDS are both in `us-east-2c`, on purpose: traffic between zones costs money and adds latency, and with one copy of each, splitting them would only add a second way to fail.
- Multi-AZ RDS keeps a standby in another zone and switches to it in a minute or two. It doubles the database's cost, so it's left off here.

## In an interview

"Why not just add servers?" Because the second server is where statelessness gets tested: here, the shared Redis and the single n8n would have to move first. "What happens if us-east-2c goes down?" Everything stops, and comes back from RDS's backups and a new instance in another zone. See `11-aws-setup.md`.
