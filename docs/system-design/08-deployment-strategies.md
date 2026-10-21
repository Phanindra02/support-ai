# Deployment strategies: recreate, rolling, blue-green, and canary

A deployment replaces the version that's running with a new one. The strategies differ in how much of the old version keeps serving while the new one starts, how much the deploy costs, and how fast a bad release can be undone. [rollback.md](../rollback.md) has this project's steps for undoing one.

## The four main strategies

### Recreate

Stop every old instance, then start the new ones.

- **Good:** simplest. Only one version ever runs, so old and new code never share the database.
- **Bad:** downtime, from the moment the old version stops until the new one is ready.
- **Rollback:** deploy the old version the same way, with the same downtime.

`docker compose up -d` does this when the image has changed. It stops the old API container and starts a new one. On one machine that's a gap of a few seconds.

### Rolling update

Replace instances a few at a time. Take one old instance out of the load balancer, start a new one, wait for it to pass its health check, then move on to the next.

- **Good:** no downtime, and no extra servers beyond one or two spare.
- **Bad:** during the rollout, old and new versions serve traffic at the same time. Both must work with the same database, and with each other's data. A bad version reaches every user by the end, unless someone stops it.
- **Rollback:** another rolling update, back to the old version. In Kubernetes: `kubectl rollout undo`.

This is a Kubernetes Deployment's default. `maxSurge` (how many extra pods may start) and `maxUnavailable` (how many may be down) set the pace, and a **readiness probe** decides when a new pod gets traffic. Without one, Kubernetes sends traffic to a pod as soon as its process starts, even if the app isn't ready.

### Blue-green

Run two full copies of the environment. Blue serves users, while green gets the new version and is tested. Then the load balancer (or DNS) moves all traffic to green at once.

- **Good:** the switch is instant, and so is a rollback, because blue is still running. Green can be tested with real infrastructure before any user sees it.
- **Bad:** twice the servers while both run. The database is usually shared, so the schema still has to work for both versions. Switching by DNS is slow, because clients cache old answers.
- **Rollback:** point the load balancer back at blue.

### Canary

Send a small share of real traffic (1 to 5%) to the new version, and watch error rates and latency against the old one. If the numbers hold, raise the share step by step to 100%. If not, send everything back.

- **Good:** a bad release only reaches a few users, and it's caught by real traffic, which finds problems tests don't.
- **Bad:** it needs traffic splitting (weighted load balancer targets, a service mesh, or a tool like Argo Rollouts) and metrics good enough to compare the two versions automatically. Like rolling updates, two versions run at once.
- **Rollback:** set the canary's share to 0%.

### Side by side

| | Downtime | Extra cost | Two versions at once | Rollback speed | Blast radius of a bad release |
| --- | --- | --- | --- | --- | --- |
| Recreate | Yes | None | No | A full deploy | Everyone |
| Rolling | No | One or two spare instances | Yes | A full rollout | Everyone, unless stopped |
| Blue-green | No | A second environment | Only during the switch | Instant | Everyone, until switched back |
| Canary | No | A few instances | Yes, for a while | Instant | The canary's share |

Two related ideas:

- **Shadow traffic** copies real requests to the new version and throws its answers away, so it's tested on real load with no user affected. Stage 7 uses this idea for models.
- **Feature flags** turn a feature on in code at runtime, for some users or all. They separate *deploying* code from *releasing* a feature, and turning a flag off is often faster than any rollback.

## The database is the hard part

Code rolls back in seconds, but data and schema don't. And every strategy except recreate runs two versions of the code against one database.

**Migrations must work with the code before them.** In this project, the `migrate` container runs `alembic upgrade head` before the new API starts. During a rolling update, old instances would still be running against the new schema. So a migration must never break the version before it.

**Expand, then contract** (also called parallel change). Rename `customer_name` to `name` without breaking anything:

1. **Expand:** add `name`. The old code ignores it.
2. Deploy code that writes both columns and reads `customer_name`.
3. Copy the old values into `name` (a backfill).
4. Deploy code that reads `name`.
5. **Contract:** once no running version uses it, drop `customer_name`.

Each step can be rolled back on its own, because the version before it still works. A single migration that renames the column breaks every old instance the moment it runs.

**Downgrades can lose data.** `alembic downgrade` reverses a migration, but dropping a column throws away what was written to it since the upgrade. It's safer to roll back the code and leave the schema where it is. That works as long as the migration was backward compatible.

## What this project does, stage by stage

- **Now (Stage 4), on the Mac:** recreate. `docker compose up --build -d` rebuilds and replaces the API in a few seconds. There's one user, the developer, so the gap doesn't matter.
- **Stage 5, one EC2 server:** still recreate. CI pushes the image, then the server pulls it and runs `docker compose up -d`. The API is down for a few seconds. Twilio retries a WhatsApp message once by default, and only if it couldn't connect at all, so a message that arrives in that gap could be lost. That's an accepted risk for a portfolio project. The fix is two instances behind a load balancer, with a rolling or blue-green switch between them.
- **Stage 8, Kubernetes:** rolling updates, with readiness probes on a `/health` endpoint, so a pod only gets traffic once it can answer. `helm rollback` undoes a release.
- **Every stage:** each image is tagged with the commit it came from, so a deploy or a rollback names an exact build. A tag like `latest` can't do that, because it moves.

## Interview questions

- **Compare rolling, blue-green, and canary.** Cost, rollback speed, blast radius, and whether two versions run at once. See the table.
- **Why do rolling updates need readiness probes?** Otherwise traffic reaches pods before the app can answer, and the rollout keeps going even when new pods are broken.
- **How do you change the database schema without downtime?** Expand and contract. Every migration must work with the code that's already running.
- **Why not just run `alembic downgrade` to roll back?** Downgrades can drop data written since the upgrade. Prefer migrations that let you roll back the code alone.
- **Your canary has a 2% error rate, and the old version has 0.5%. What now?** Send all traffic back to the old version first, then investigate. Better still, have the rollout tool compare the two and roll back automatically.
- **Why tag images with the commit SHA and not just `latest`?** A SHA names one exact build forever, so you know what's running and can redeploy the one before it. `latest` changes under you.
- **How is a feature flag different from a deploy?** A deploy ships code, and a flag decides who sees it. Flags let you turn a feature off without redeploying, but each one adds a branch to the code that someone has to clean up.
