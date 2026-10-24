# Rolling out a new model safely

A new model can beat the old one on the test set and still do worse on live tickets. Live text may differ from the training text, a bug may clean text one way in training and another when serving, or the scores may shift and change how many alerts go out. So a new version earns its traffic in steps, any of which can be undone.

## Shadow testing

The new version runs beside the current one on every real ticket, but only the current version's answer is used. The new one's answer is just saved. Customers and agents see nothing change.

Afterwards the two are compared:

- How often they agree.
- Once tickets close, which one matched the priority the agent settled on more often.
- Whether the new one is slower, or fails.

It costs a second prediction per ticket, which for these models is a millisecond. Its limit is that it can't show effects that only happen when the answer is used, such as agents trusting a new version's priorities more.

## Gradual traffic shifts

Once the shadow results look good, the new version gets a small share of tickets for real, say 10%, then 50%, then all of them. Between steps, the team watches the error rate, how often agents change the priority, and the alert volume for that version's tickets. This is a canary release (`08-deployment-strategies.md`), applied to a model.

The split should be stable, such as by `ticket_id % 10`, so each ticket gets one version. Rolling back means sending 0% to the new version.

## Apply it: switching the priority model to v2 without downtime

Nothing in the request path calls the model service. Tickets queue a job, and the worker retries a failed call after 5 seconds, 30 seconds, and 2 minutes. So the worst a restart can do is delay a priority by a few seconds. No ticket or reply is lost. The steps:

1. **Train and save v2.** Change the priority entry in `ml/save.py` to version 2 with the new settings or data, then run `uv run python -m ml.save --upload`. That writes `models/priority/v2/`, and `v1` stays as it was.
2. **Check it offline.** v2's `test_macro_f1` in `metadata.json` should beat v1's 0.709 on the same test split. Its `sklearn_version` must match the service's, or it won't load.
3. **Shadow it for a week.** This part isn't built yet. A `SHADOW_PRIORITY_MODEL_VERSION=2` setting would make the model service load v2 next to v1 and return both answers. The worker would act on v1's and save v2's as a `predictions` row with `version=2`. Then compare the two versions with the export.
4. **Switch.** Set `PRIORITY_MODEL_VERSION=2` and replace the container (`docker compose up -d model`). The new container downloads v2 and only answers `/health` once it has loaded. Jobs that hit the few seconds' gap fail and are retried. For no gap at all, start the v2 container next to the old one, wait for its `/health`, point `MODEL_SERVICE_URL` at it, then stop the old one. In Stage 8, Kubernetes does exactly this as a rolling update: a new pod gets traffic only once its readiness probe (`/health`) passes, and the old pod stops after that.
5. **Watch, and roll back if needed.** If agents change v2's priorities more often than v1's, or alerts jump, set the version back to 1 and replace the container again. Nothing else changes: v1's files are still in S3, and every prediction says which version made it.

For a gradual shift instead of step 4, the service would load both versions, and the worker would ask for v2 on one ticket in ten, then on more.
