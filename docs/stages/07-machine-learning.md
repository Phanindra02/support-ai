# Stage 7: Machine learning

## What was built

Every new ticket now gets its priority from a trained model, and an angry first message or a high priority alerts the team in Slack, without an agent touching it.

1. **Data.** Two Kaggle datasets in the git-ignored `ml/data/`: 16,338 English support tickets labeled low, medium, or high, and 14,640 airline tweets labeled negative, neutral, or positive. `ml/explore.py` measured class balance, length, and missing values (`docs/results.md`).
2. **Preparation.** `ml/prepare.py` cleans the text with `ml/text.py` (lowercase, no links, @handles, or repeated characters) and holds back a stratified 20% as the test set, with a fixed seed.
3. **Priority model.** A TF-IDF and Logistic Regression baseline reached macro F1 0.594. XGBoost on the same features plus length scored lower (0.568) and trained 12 times slower. `ml/tune.py` tuned both with 3-fold cross-validation, with and without class weights. Plain Logistic Regression with C=30 won, at test macro F1 0.709.
4. **Sentiment model.** The same recipe on the tweets: weighted Logistic Regression with C=1, macro F1 0.743. When it says negative, it's right 88.8% of the time.
5. **Saved and versioned.** `ml/save.py` writes `model.joblib` and `metadata.json` (settings, test scores, git commit, scikit-learn version) to `models/<name>/v1/` in S3. A saved version never changes.
6. **Model service.** `ml/service.py` is a separate FastAPI app with `/predict/priority`, `/predict/sentiment`, and `/health`. It loads the versions its settings name, downloading them from S3 the first time. Each answer has the label, every label's score, and the model version. It runs from its own image (`infra/model.Dockerfile`), which CI pushes to the `support-ai-model` ECR repository, and it runs on the EC2 server next to the API.
7. **Triage job.** Every new ticket, from the API or WhatsApp, queues `worker.jobs.triage_ticket`. It sends the subject and first message to `/predict/priority` and the first message to `/predict/sentiment`, saves the priority and both predictions in one commit, and alerts through the Stage 3 n8n workflow when the priority is high or "negative" scores 0.8 or more.
8. **Data for retraining.** Predictions go in a `predictions` table with their version. `scripts/export_predictions.py` writes each closed ticket's text, predicted priority, and the priority the agent left it at, as CSV.

## Key decisions, and why

- **Logistic Regression over XGBoost.** It scored higher on these features, trains in seconds, and its weights can be read: the words that push a ticket toward high can be listed. XGBoost shines on tabular features. On thousands of sparse word counts, a linear model is hard to beat.
- **Macro F1, not accuracy.** Low priority is a fifth of the tickets. A model that never says low can still score well on accuracy. Macro F1 averages the three classes equally, so the rare one counts.
- **Class weights, tried and kept only where they helped.** They make a mistake on a rare-class example cost more in training, which is one setting instead of resampling the data. They lifted XGBoost's low recall from 0.253 to 0.578. For the priority model, tuning C did most of the work, and weights changed its cross-validation score by 0.002, which is noise, so the plain model stays. The sentiment model keeps them.
- **A separate model service, not models inside the API.** scikit-learn, SciPy, and NumPy add about 300 MB, so the API image stays small. The models can be updated, restarted, or scaled without touching the API, and the service is the only thing that loads them.
- **Predictions in a background job.** Nothing the customer waits on calls the model. If the service is down, the ticket stays at medium and the job tries again after 5 s, 30 s, and 2 min. Both calls finish before anything is saved, so a retry never sends a second alert.
- **0.8 for "very negative".** Measured on the test tweets: 99.1% of flagged messages are really negative, and only the angriest fifth alert. At 0.5, more than half of all messages would alert, and the team would learn to ignore the channel.
- **Versions in the path, never overwritten.** `models/priority/v1/` always means the same file. Rolling back is changing a number, and every saved prediction says which version made it.
- **Same cleaning code in training and serving.** Both import `ml/text.py`. If they cleaned text differently, the model would see different input in production than in training, and the test scores would mean nothing.

## How to run and demo it

```bash
uv run python -m ml.explore                  # class balance, length, missing values
uv run python -m ml.train tickets baseline   # or xgboost, or tweets instead of tickets
uv run python -m ml.tune tickets             # cross-validated tuning, about 5 minutes
uv run python -m ml.save                     # save both models to ml/models/ (--upload sends them to S3)
docker compose up -d --build                 # API, worker, and model service
docker compose exec model python -c "import urllib.request as u; print(u.urlopen(u.Request(\
  'http://localhost:8000/predict/sentiment', b'{\"text\": \"Third time asking. Useless.\"}',\
  {'Content-Type': 'application/json'})).read().decode())"
uv run python -m scripts.export_predictions  # closed tickets' predicted and final priority
```

The model service publishes no port, since only the API and the worker call it, so the demo asks it from inside its container. On the live server, open a ticket through https://support.mysolospark.com/docs, or send an angry WhatsApp message. A few seconds later, the ticket's priority has changed and Slack has the alert, with the reason in "Marked urgent by", such as "the model: a very negative customer (94% sure)".

## What an interviewer might ask

- **Why not use an LLM to set priority?** It would cost money and a second or more for every ticket, and its answers can drift when the provider changes the model. A trained classifier answers in milliseconds for free, gives a score for each class, and is measured on 3,268 held-out tickets.
- **How do you know the model is good?** Precision, recall, and F1 for each class on a test set it never saw, compared against a simple baseline. The test set is split once, with a fixed seed, and tuning uses cross-validation on the training set only, so the test score isn't tuned to.
- **What's the difference between precision and recall here?** Precision: of the tickets called high, how many really are. Recall: of the really high tickets, how many were caught. The alert threshold trades one for the other.
- **The sentiment model was trained on airline tweets. Is that a problem?** Yes, a little. A calm outage report scored 0.82 negative because outage words read as anger in tweets. The predictions table is how that gets fixed: once agents close enough tickets, there's real support data to retrain on.
- **How would you update a model without downtime?** Save v2 next to v1, change the version setting, and restart the model service. Nothing in the request path calls it, and the worker retries, so a restart only delays a prediction by seconds. Shadow testing first runs v2 next to v1 and compares (`docs/system-design/15-model-rollouts.md`).
- **How would you notice the model getting worse?** Watch the share of each predicted priority and how often agents change the model's priority. A sudden shift means the tickets changed (drift) or the model did.
- **What if the model service is down?** Tickets still open, at medium, and customers notice nothing. The job retries, then waits in RQ's failed list, which `scripts/failed_jobs.py` can requeue.
- **Did it fit on the server?** On the 2 GB t4g.small, the model container used 149 MB and left 736 MB available. It went on the server only after measuring that, and after its image was in ECR so the server never builds.
