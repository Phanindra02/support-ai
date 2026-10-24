# Serving models: batch or real-time, versions, and drift

The project has two models: priority (Logistic Regression on TF-IDF, macro F1 0.709) and sentiment (macro F1 0.743). This note covers when they run, how a version is pinned down, and how to tell when they stop matching real tickets.

## Batch vs real-time predictions

A **batch** prediction runs on many rows at once, on a schedule: say, every night over the day's tickets. A **real-time** prediction runs on one row as it arrives.

Batch is cheaper per row and simpler to run. Nothing has to be up all the time, and a failed run just runs again. But its answers are hours old. Real-time answers in seconds, which costs an always-on service that has to stay fast and up.

Priority and anger have to be real-time here. An alert about an angry customer is only useful while the team can still help. A nightly batch would find out the next morning.

The project's real-time predictions still stay out of the request. A new ticket queues `triage_ticket`, and the worker calls the model service a moment later (`worker/jobs.py`). The customer's WhatsApp reply never waits on a model, and a model service that's down delays a priority rather than failing a ticket. This is sometimes called near-real-time, or asynchronous real-time.

Batch would still fit work that isn't urgent, such as adding each week's sentiment trend to the weekly report (`scripts/weekly_report.py`).

## Model versioning

A model is code plus data plus settings, so "the priority model" means nothing unless it names which one. Here, each saved model is a folder that is never changed again: `models/priority/v1/` in S3, with:

- `model.joblib`, the trained pipeline.
- `metadata.json`, holding the settings, the training rows, the test scores, the git commit it was trained from, and the scikit-learn version.

A new model gets `v2` and leaves `v1` alone. That makes a rollback trivial, since the old files are still there.

The model service loads the version its settings name (`PRIORITY_MODEL_VERSION`), and it won't load a file saved by a different scikit-learn version, because pickles don't reliably load across versions. Every prediction the worker saves records the version that made it (the `predictions` table). So any decision can be traced back to an exact file and commit.

## Monitoring for drift

A model learns from the past. **Data drift** is when new inputs stop looking like the training data, such as a new product with new words. **Concept drift** is when the right answer itself changes, such as the team deciding that every billing issue is now high. Either way, the test score stops describing reality.

The `predictions` table makes four checks possible:

1. **The share of each predicted label per week**, against the training shares (medium 40.5%, high 38.8%, low 20.7%). A sudden jump to 60% high is worth a look.
2. **Average confidence**, which is the winning label's score. Every score is stored, not just the label, because a model that grows less sure is often seeing unfamiliar text.
3. **How often agents change the priority** on closed tickets (`scripts/export_predictions.py`). This is the real accuracy, though it's only known once tickets close. With a test accuracy of 0.720, about 28% changes is the expected baseline. A rising rate means it's time to retrain.
4. **Alert volume.** The 0.8 bar flagged a fifth of the test tweets. Far more alerts than that means the sentiment model is misreading support tickets.

The sentiment model already has known drift built in. It learned from airline tweets, and it reads a calm outage report as negative (0.82). Retraining on the team's own labeled tickets, from the export, is the fix.
