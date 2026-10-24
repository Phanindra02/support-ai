# Roadmap

The build plan for Support AI, converted from the developer's checklist on October 18, 2026. Work through it in order, and tick each box (`- [x]`) in the same commit that finishes the step.

Steps marked **[You]** need the developer: their accounts, keys, payment, or their own practice. Prepare everything around those steps, then stop and give exact instructions.

Each stage opens with what should work when it's done.

## Stage 0: Setup

Done when: tools are installed and the repo with the folder layout is on GitHub.

- [x] Install Python, Git, VS Code, and Node.js (n8n needs it).
- [x] Install Homebrew.
- [x] Create the GitHub repo (`Phanindra02/support-ai`) and a virtual environment with uv.
- [x] Add a `.gitignore` and a README.
- [x] Create the folders `app/`, `worker/`, `ml/`, `lambda/`, `infra/`, and `tests/`.

## Stage 1: Ticket API with FastAPI and Pandas

Done when: a tested ticket API runs locally on SQLite, and a Pandas script produces a weekly report.

- [x] Install FastAPI, Uvicorn, SQLAlchemy, and Pydantic.
- [x] Run a hello-world endpoint and open `/docs`.
- [x] Define a ticket: id, customer name, phone, subject, message, status (open, pending, closed), priority (low, medium, high, urgent), created time, and closed time.
- [x] Store tickets in SQLite through SQLAlchemy.
- [x] Build endpoints to create a ticket, list tickets (with filters and pagination), get one ticket, and update status or priority.
- [x] Validate input with Pydantic schemas, and return proper errors (404 for a missing ticket, 422 for bad input). The developer may already have applied a tested version of this, so check `app/schemas.py` and `git status` first. The rules:
  - `customer_name` is 1–100 characters, `subject` is 1–200, and `message` is 1–5,000, all after trimming spaces (`str_strip_whitespace=True`).
  - `phone` matches E.164: `^\+[1-9][0-9]{1,14}$`. Use `[0-9]`, not `\d`, which also matches digits from other alphabets.
  - `TicketCreate` and `TicketUpdate` use `extra="forbid"`.
  - A PATCH must include `status`, `priority`, or both. A `model_validator` returns a 422 saying "Send status, priority, or both."
  - `TicketRead` is a standalone model with no input rules.
  - `ticket_id` uses `Path(ge=1, le=MAX_INT)`, and `skip` uses `le=MAX_INT`, where `MAX_INT` is 2,147,483,647. Huge numbers get a 422 instead of crashing with a 500.
  - An `ErrorResponse` model (`detail: str`) is listed as the 404 response on the get-one and update endpoints, and the schemas carry `json_schema_extra` examples for `/docs`.
- [x] Write pytest tests for every endpoint using FastAPI's TestClient. Override `get_db` with a fresh test database, and never touch `support.db`. Cover the validation rules above, including boundary cases.
- [x] Write a seed script that uses Faker to create about 1,000 realistic fake tickets. Spread `created_at` over the last 12 weeks, and close most older tickets with realistic `closed_at` times, so the weekly report has real numbers.
- [x] Write a Pandas script that reports weekly ticket volume, average time to close, and open tickets by priority, saved as a CSV.

### System design

- [x] Learn how a request travels from browser to server: DNS, HTTP, and load balancers. The notes file `docs/system-design/01-request-journey.md` hasn't been written yet.
- [x] Learn REST API design: resource names, status codes, pagination, idempotency, and versioning.
- [x] Apply it: document the ticket endpoints, status codes, and pagination in the README.

## Stage 2: PostgreSQL and Redis

Done when: the API runs on Postgres with versioned migrations, measured index speedups, and Redis caching and rate limiting.

- [x] Install PostgreSQL with Homebrew and start it as a background service. Then create a database and user, and point SQLAlchemy at it through `DATABASE_URL` in `.env`.
- [x] Add Alembic so every schema change is a versioned migration, replacing `Base.metadata.create_all`.
- [x] Expand the schema into customers, agents, tickets, and messages (the back-and-forth on each ticket), linked with foreign keys.
- [x] Seed 100,000 tickets and run `EXPLAIN ANALYZE` on the list query.
- [x] Add indexes on status, priority, and created time, measure again, and record the before and after numbers in `docs/results.md`. Priority was measured and left out: the planner never used it (see `docs/results.md`).
- [x] Install Redis with Homebrew, start it as a background service, and cache a stats endpoint (open ticket counts) for 60 seconds.
- [x] Clear the cache whenever a ticket changes.
- [x] Add rate limiting with Redis, such as 60 requests per minute per IP.
- [x] Point the tests at a separate test database.

### System design

- [x] Learn how databases scale: indexes, read replicas, sharding, and SQL vs NoSQL trade-offs.
- [x] Learn caching patterns (cache-aside, write-through, eviction) and rate-limiting algorithms (token bucket, sliding window).
- [x] Apply it: note which caching pattern and rate-limiting algorithm the project uses, and why.

## Stage 3: Auth, Twilio, and n8n

Done when: agents log in with JWTs, WhatsApp messages become tickets, and urgent tickets post to Slack through n8n.

- [x] Add agent accounts with hashed passwords and a login endpoint that returns a JWT, following FastAPI's official security tutorial.
- [x] Require login for agent endpoints, and add two roles: agents update tickets, and admins manage agents.
- [x] **[You]** Create a Twilio account and join the WhatsApp sandbox (not plain SMS; see the note below). Put the account SID and auth token in `.env`. Only the auth token went in: nothing uses the SID until the API sends messages itself.
- [x] Install ngrok with Homebrew and run it so Twilio can reach the API. **[You]** Create an ngrok account and add its authtoken.
- [x] **[You]** Point the sandbox's incoming-message webhook at `POST /webhooks/twilio` on the ngrok URL.
- [x] In that webhook, verify Twilio's request signature.
- [x] Find or create the customer by phone number, and add the message to their open ticket or a new one.
- [x] Send the customer a confirmation reply.
- [x] Start n8n with `npx n8n` and build a workflow: a Webhook trigger that posts ticket details to a Slack channel. Commit the exported workflow JSON in `infra/n8n/`. **[You]** Create the Slack channel and connect Slack in n8n.
- [x] When a ticket is marked urgent, have the API call that n8n webhook.
- [x] Add a shared secret header to that call so only the API can trigger it.
- [x] Write tests that fake the Twilio and n8n calls.

Why WhatsApp: plain SMS to US numbers needs A2P 10DLC registration, trial accounts can't register, and reviews take 10–15 days. WhatsApp skips that registration.

### System design

- [x] Learn authentication vs authorization, sessions vs JWTs, and the main OAuth2 flows.
- [x] Learn how webhooks handle retries and duplicate deliveries, and why handlers should be idempotent.
- [x] Apply it: make the Twilio webhook skip message IDs that are already stored, so a repeat delivery does nothing.

## Stage 4: Docker and CI/CD

Done when: one command starts the whole stack in containers, and GitHub Actions tests every push and publishes an image.

- [x] **[You]** Install Docker Desktop for Mac and start it. Then write a Dockerfile for the API using a slim Python image, uv, and a non-root user.
- [x] Write a `docker-compose.yml` that starts the API, Postgres, Redis, and n8n with one command.
- [x] Move all settings and secrets into the git-ignored `.env`, keep `.env.example` current, and add a `.dockerignore`. The `.dockerignore` came with the Dockerfile, which needed it.
- [x] Add a GitHub Actions workflow that runs on every push: install dependencies with uv, lint with ruff, and run pytest.
- [x] Run those tests against a Postgres service container inside the workflow. Redis runs as one too, since the tests need it.
- [x] On merges to main, build the image on GitHub's ARM runner (`ubuntu-24.04-arm`) and push it to GitHub Container Registry. Published as `ghcr.io/phanindra02/support-ai-api`; anyone can pull it, since the repo is public.
- [x] Protect the main branch so nothing merges unless CI passes. From here on, work on branches and open pull requests. The rules are in `infra/github/main-protection.json`, and apply to admins too.

### System design

- [x] Learn containers vs virtual machines, and why config belongs in environment variables.
- [x] Learn deployment strategies: rolling, blue-green, and canary.
- [x] Apply it: write down how to roll back a bad release. See `docs/rollback.md`.

## Stage 5: AWS

Done when: the app runs on EC2 over HTTPS with RDS and S3, a Lambda processes uploads, and merges to main deploy automatically.

Every resource in this stage costs money. Give the expected monthly cost and get a yes before creating anything, and ask which region to use.

- [x] **[You]** Create an AWS account and turn on MFA for the root user. The account is on the Paid plan, which never suspends at six months, and the region is `us-east-2` (Ohio).
- [x] **[You]** Create an IAM admin user for daily work and set a monthly budget alert. Install the AWS CLI with Homebrew, then **[You]** run `aws configure`. The user is `phani-admin`, with MFA, and the budget is $20 a month.
- [x] Create a private S3 bucket for help docs. It's `support-ai-docs-phanindra02`, and `infra/aws/README.md` has how it was made.
- [x] Add `POST /documents` so an agent can upload a PDF.
- [x] Save uploads to S3 with boto3, and record each one in a documents table as "uploaded." Built in the same commit as the endpoint, which has nothing to return until an upload is saved.
- [x] Create an ECR repository and switch CI to push images there. CI logs in with GitHub's OIDC token, so no AWS keys are stored in GitHub. See `infra/aws/README.md`.
- [x] Launch a Graviton EC2 instance (t4g family) with an IAM role for S3 and ECR, install Docker, and run the stack with docker compose. A t4g.small running Ubuntu 24.04, pulling CI's image from ECR.
- [x] Create the smallest RDS PostgreSQL instance, and allow connections only from the EC2 instance. A `db.t4g.micro` on PostgreSQL 16.15 with no public address, whose security group lets in only the server's security group. The URL with its password is in Parameter Store.
- [x] Run the Alembic migrations on RDS and switch the API over to it. `infra/aws/compose.ec2.yml` points the server at RDS, and its one agent was copied over with `pg_dump`. The API connects with TLS 1.3 and checks the certificate (`sslmode=verify-full`).
- [x] Store secrets in AWS Systems Manager Parameter Store and load them when the app starts. SecureStrings under `/support-ai`, loaded by a settings source in `app/config.py` on EC2 only. The server's `.env` holds no secrets.
- [x] **[You]** Buy a cheap domain. Point it at the instance, and put Caddy in front of the API for free automatic HTTPS. The API is at `https://support.mysolospark.com`, a DNS-only Cloudflare record pointing at the Elastic IP. Caddy runs on the server's own network and reaches the API at `127.0.0.1:8000`, so the API's trust in forwarded headers didn't change.
- [x] **[You]** Update the Twilio webhook to the new URL. The sandbox calls `https://support.mysolospark.com/webhooks/twilio`. The first message made ticket 1 on RDS, and its signature passed, so behind Caddy the API rebuilds the `https://` URL that Twilio signed.
- [x] Write a Lambda function that runs on every S3 upload and extracts the PDF's text with pypdf. `support-ai-extract-text` (`lambda/extract_text.py`), on Python 3.14 and Graviton, runs for each new `uploads/*.pdf`.
- [x] Have the Lambda save the text as a file in S3, then call the API (with a shared secret) to mark the doc "processed." The text goes to `text/<id>.txt`, and `POST /webhooks/document-processed` checks the secret from Parameter Store. Built with step 13, which has nothing to show until the text is saved. The first real upload was processed within 2 seconds.
- [x] Extend CI so merges to main push to ECR, then SSH into EC2 to pull and restart the new image. **[You]** Add the deploy secrets in the GitHub repo settings. Through AWS Systems Manager instead of SSH, the developer's choice: no port opened and no key in GitHub, so there were no deploy secrets to add, just the repo variable `SERVER_INSTANCE_ID`. The first deploy (run 40034718281) put `sha-9d9b9af` on the server.

Why Graviton: it runs on ARM like the Mac, so images built locally run on it as-is, and it usually costs less than a standard instance.

### System design

- [x] Learn vertical vs horizontal scaling, load balancers, stateless services, and availability zones. See `docs/system-design/09-scaling-and-availability.md`.
- [x] Learn when to use object storage, a CDN, and serverless functions like Lambda. See `docs/system-design/10-storage-cdn-serverless.md`.
- [x] Apply it: draw the AWS setup and mark every single point of failure. See `docs/system-design/11-aws-setup.md`.

## Stage 6: RAG with LangChain and pgvector

Done when: WhatsApp questions get answers from the docs with sources, and accuracy is measured on 25 test questions.

- [x] **[You]** Get an OpenAI API key. Use it for both chat and embeddings, so there's one provider. OpenAI accepts it: `GET /v1/models` answers 200 from the Mac and from the server.
- [x] **[You]** Store the key in Parameter Store, and in `.env` for local work. It's the SecureString `/support-ai/OPENAI_API_KEY`, copied from `.env` without being shown. The server's API loaded it when the deploy of `sha-4ceb23e` restarted it, and it isn't in the container's environment.
- [x] Turn on pgvector with `CREATE EXTENSION vector;` on RDS. Locally, switch the compose Postgres image to `pgvector/pgvector`. A migration runs it, so it ran on RDS with the deploy of #19.
- [x] Add a chunks table with document id, chunk text, and embedding. Each chunk also has its `position` in the document, unique per document, so a second run of the worker can't save every chunk twice. Embeddings are `vector(1536)`, the size of OpenAI's `text-embedding-3-small`.
- [x] Add a background worker using RQ, on the Redis the project already uses. `worker/main.py`, run by compose's `worker` service from the API's image. Redis now saves every write to a volume (append-only), so queued jobs survive a restart.
- [x] When a doc is marked processed, have the API queue a job for it. Every call queues it, a repeat too, so if Redis was down the Lambda's retry queues it. Built with step 7, since the job has nothing to do until it reads the text.
- [x] In the worker, load the text from S3 and split it with LangChain's text splitter (start at about 1,000 characters with some overlap). `RecursiveCharacterTextSplitter`, at 1,000 characters with 200 of overlap, cuts between paragraphs where it can. The server's role may now read under `text/`.
- [x] Embed each chunk and save it to pgvector. LangChain's `OpenAIEmbeddings` with `text-embedding-3-small`, all of a document's chunks in one call. The job replaces the document's chunks in one transaction, so a second run leaves one copy, and a failed OpenAI call changes nothing.
- [x] Build `POST /ask`: embed the question and pull the 4 closest chunks. pgvector orders chunks by cosine distance to the question. Only logged-in agents may ask, since every question costs an OpenAI call; customers reach the bot through WhatsApp.
- [x] Send those chunks to the LLM with instructions to answer only from them and say "I don't know" otherwise. `gpt-4.1-mini` at temperature 0, with the rules in the system message and the customer's words in their own. Built with steps 10 and 12 in `app/bot.py`, since none is useful alone.
- [x] Return the answer along with the names of the source docs. Each doc once, closest first, and none when the bot doesn't know.
- [x] Route WhatsApp messages to the bot first. A customer whose ticket the team is already working on keeps talking to the team. Each OpenAI call gets 6 seconds and no retries, so the reply fits in the 15 seconds Twilio waits.
- [x] If the bot doesn't know, open a ticket for a human and tell the customer someone will follow up. The same happens when OpenAI fails or the message has no text, so a customer always gets a reply. Built with step 13, since the old ticket path is the bot's fallback.
- [x] Cache answers in Redis, keyed by the cleaned-up question text. Lowercased, single-spaced, and without ?, ! or . at the end, then hashed. The key also holds a docs version the worker bumps whenever a document's chunks change, so new docs make every old answer stale at once. Kept for a day.
- [x] Write 25 test questions with known answers. `docs/help-docs/questions.json`, about three sample help docs for a made-up store (refunds, shipping, account and payments), each a `.txt` and the `.pdf` made from it with `cupsfilter`. 24 are answered in the docs, and 1 isn't, to check the bot says "I don't know."
- [x] Measure how often the right chunk comes back and the answer is correct, and record it in `docs/results.md`. `scripts/measure_bot.py`: retrieval 24/24, answers 25/25, sources 25/25, after fixing a bug the first run found.
- [x] Tune chunk size and chunk count, and record the results. Five settings all scored perfectly, so the test set is too easy to separate them; kept 1,000 characters and 4 chunks.

### System design

- [x] Learn message queues and background jobs: retries, at-least-once delivery, and dead-letter queues. `docs/system-design/12-message-queues.md`.
- [x] Learn how LLM apps control latency and cost: caching, batching embeddings, and limiting context size. `docs/system-design/13-llm-latency-and-cost.md`.
- [x] Apply it: give worker jobs a retry limit, and keep failed jobs in a separate list that can be inspected. 3 more tries after 30 s, 2 min, and 10 min, then RQ's failed job registry, which `scripts/failed_jobs.py` lists and can requeue.

## Stage 7: Machine learning

Done when: a model service sets each new ticket's priority, flags angry customers, and triggers the Slack alert.

- [x] **[You]** Download a support-ticket dataset with priority labels from Kaggle. Suggest a dataset and the exact download steps first. Tobias Bueck's "Multilingual Customer Support Tickets", English rows only, in the git-ignored `ml/data/`.
- [x] Explore it in Pandas: class balance, text length, and missing values. `ml/explore.py`, with the numbers in `docs/results.md`.
- [x] Clean the text and split it into training and test sets, keeping the class balance the same in both. `ml/prepare.py` lowercases, drops links, @handles, and repeats, then holds back a stratified 20% with a fixed seed.
- [x] Build a baseline with TF-IDF and Logistic Regression, and record precision, recall, and F1 for each priority level. `ml/train.py`: accuracy 0.638 and macro F1 0.594, with low priority caught only 31% of the time.
- [x] Train XGBoost on the TF-IDF features plus simple extras like message length. Macro F1 0.568, below the baseline's 0.594, and 12 times slower to train.
- [x] Tune a few settings with cross-validation, give rare priority levels extra weight, and compare against the baseline. `ml/tune.py`: Logistic Regression with C=30 wins, at test macro F1 0.709 against the baseline's 0.594. Weighting lifted XGBoost's low recall from 0.253 to 0.578, but it still trails.
- [x] Train a sentiment model the same way on labeled customer messages, such as Kaggle's airline tweet sentiment dataset. **[You]** Download it. Downloaded with the tickets. Weighted Logistic Regression with C=1 wins, at test macro F1 0.743, and it's right 88.8% of the times it says negative.
- [x] Save both models with joblib, give each a version number, and store them in S3. `ml/save.py` writes `model.joblib` and `metadata.json` (settings, test scores, git commit, and scikit-learn version) to `models/<name>/v1/` in the help docs bucket. A saved version is never changed; a new model gets a new number.
- [x] Build a separate FastAPI model service with `/predict/priority` and `/predict/sentiment`, and containerize it. `ml/service.py`, in `infra/model.Dockerfile`, as compose's `model` service. Each prediction gives the label, every label's probability, and the model version. It cleans text with the same `ml/text.py` as training. It stays off the server until its image is in ECR.
- [x] When a new ticket arrives, have the worker call the model service and set the priority. Every new ticket, from the API or WhatsApp, queues `worker.jobs.predict_priority`, which sends the subject and first message to `/predict/priority` and saves the label. A failure leaves the ticket at medium and tries again after 5 s, 30 s, and 2 min. It's off on the server until the model service runs there.
- [x] Trigger the n8n alert if priority is high or sentiment is very negative. The worker's job, now `triage_ticket`, also asks `/predict/sentiment` about the first message, and alerts through the same n8n workflow when the priority is high or "negative" scores 0.8 or more. The reason fills the Slack message's "Marked urgent by", so the workflow didn't change.
- [ ] Log every prediction next to the priority the agent finally set, as data for retraining later.

### System design

- [ ] Learn batch vs real-time predictions, model versioning, and monitoring for drift.
- [ ] Learn safe model rollouts: shadow testing and gradual traffic shifts.
- [ ] Apply it: write down how to switch the priority model to a new version without downtime.

## Stage 8: Kubernetes and EKS

Done when: the API, worker, and model service autoscale on EKS behind HTTPS, CI deploys them, and load-test numbers are recorded.

EKS bills by the hour. Get a yes before creating the cluster, and delete it as soon as the demo is recorded.

- [ ] Install minikube, kubectl, Helm, and eksctl with Homebrew, and add a `/health` endpoint to each service.
- [ ] Write Deployments and Services for the API, worker, model service, and Redis. n8n can stay on EC2.
- [ ] Add ConfigMaps for settings and Secrets for keys.
- [ ] Add readiness and liveness probes, CPU and memory limits, and a HorizontalPodAutoscaler for the API.
- [ ] Get everything running on minikube, then package it as a Helm chart.
- [ ] Create an EKS cluster with eksctl (two small Graviton nodes, such as t4g.medium), and keep RDS as the database.
- [ ] Give pods S3 access through an IAM role using EKS Pod Identity, so no AWS keys live in the cluster.
- [ ] Install the AWS Load Balancer Controller and create an HTTPS Ingress with a free ACM certificate.
- [ ] **[You]** Point the domain and the Twilio webhook at the new Ingress.
- [ ] Update CI to deploy with `helm upgrade` on merges to main.
- [ ] Load test the API with Locust, watch the autoscaler add pods, and record the numbers in `docs/results.md`.
- [ ] **[You]** Record a demo, then run `eksctl delete cluster` so it stops billing.
- [ ] **[You]** Point the domain and Twilio webhook back to EC2, the always-on version.

### System design

- [ ] Learn service discovery, health checks, autoscaling, and observability: logs, metrics, and traces.
- [ ] Learn timeouts, retries with backoff, and circuit breakers between services.
- [ ] Apply it: add timeouts and retries to the API's calls to the model service.

## Stage 9: System design practice

Done when: the developer can design a common system out loud in 45 minutes, and the platform has a written design doc.

- [ ] Write a design doc for the platform in `docs/design-doc.md`: requirements, scale estimates, architecture diagram, data model, bottlenecks, and trade-offs. **[You]** Review it until you can defend every part.
- [ ] **[You]** Learn the interview flow and quick estimates, then do timed 45-minute designs of a URL shortener, a rate limiter, a notification system, a chat app, a news feed, and Dropbox-like file storage.
- [ ] **[You]** Redesign the platform for 10 million users, and compare it to what you built.
- [ ] **[You]** Do at least three mock interviews.

## Finish

Done when: someone can understand, run, and see the project in a few minutes, and the resume uses real numbers.

- [ ] Write the README with an architecture diagram, setup steps, screenshots, and a link to the design doc.
- [ ] **[You]** Record a 2–3 minute demo: ask a question on WhatsApp and get an answer from the docs, then send an angry message and show the Slack alert.
- [ ] List the real numbers from `docs/results.md`: index speedup, cache latency, model F1, retrieval accuracy, and load-test results.
- [ ] Draft resume bullets from those numbers. **[You]** Edit them into your resume.

## The developer's own practice (not for Claude Code)

- Sundays: Blind 75, about four problems a week, in the separate `blind75` repo.
- Fridays: Alex Xu's *System Design Interview*, Volume 1.
