# Resume bullets

Drafts for the resume, built only from numbers in [results.md](results.md). Pick three or four, and lead with the ones that fit the job: the bot and the models for AI roles, the database, deploys, and load test for backend roles.

## The project line

**Support AI**: AI customer support platform on WhatsApp. Python, FastAPI, PostgreSQL with pgvector, Redis, LangChain with OpenAI, scikit-learn, AWS, Kubernetes. github.com/Phanindra02/support-ai

## Bullets

- Built a WhatsApp support bot (Twilio, FastAPI) that answers from uploaded help docs with retrieval-augmented generation (LangChain, OpenAI, pgvector). On a 25-question test set it retrieved the answer for 24 of 24 answerable questions and replied correctly with the right source every time, saying "I don't know" instead of guessing.
- Trained ticket priority and customer sentiment classifiers (scikit-learn, XGBoost) to macro F1 of 0.709 and 0.743. Cross-validated tuning raised priority F1 from a 0.594 baseline, and the angry-customer alert flags messages at 99.1% precision.
- Made the ticket list query on 100,000 tickets about 550x faster (11 ms to under 0.1 ms) with a PostgreSQL index chosen by reading EXPLAIN ANALYZE plans, and made dashboard counts 6 to 8x faster with a Redis cache.
- Deployed to AWS (Graviton EC2, RDS, S3, Lambda, ECR, Parameter Store) for about $31 a month, with GitHub Actions running more than 750 tests on every pull request and deploying each merge through SSM. OIDC and IAM roles mean no AWS keys are stored anywhere.
- Ran the services on Kubernetes (EKS, Helm) behind HTTPS with autoscaling. A Locust load test sustained 182 requests a second at a 34 ms median and 130 ms 95th percentile, with 1 error in 87,244 requests, as the API scaled from 2 to 6 pods in about a minute.
- Moved slow work (embedding docs, ML triage, Slack alerts through n8n) onto an RQ background worker with retries and timeouts, so customers still get a reply when OpenAI, Redis, the model service, or n8n is down.

## Be ready to explain

- **Why the bot's score is perfect.** The test set is three short docs and 25 questions, so every setting scored the same. It shows the bot doesn't make things up, not that it beats other settings.
- **Why 0.709 is good enough.** Priority is a three-way choice on noisy ticket text, and the model only sets a starting priority that agents can change. Every prediction is saved, so it can be checked against what agents settled on.
- **Why the 550x needs care.** Times under 0.1 ms are near the smallest EXPLAIN can measure. The deep page only got about 3x faster, since `OFFSET` still reads the skipped rows, and cursor pagination is the fix.
- **Where the load test ran from.** A Mac at home, so every time includes the round trip to us-east-2. The rate limit was raised for the test, because every request came from one address.
