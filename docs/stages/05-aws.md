# Stage 5: AWS

The app now runs on AWS: one Graviton EC2 server behind HTTPS at `https://support.mysolospark.com`, a managed Postgres database (RDS), help docs in S3, a Lambda that pulls the text out of each upload, secrets in Parameter Store, and a deploy after every merge to `main`.

## What was built

- **Account and guard rails:** root MFA, an IAM admin user with MFA, and a monthly budget alert, in `us-east-2`.
- **S3:** a private bucket for help docs: every public-access block on, HTTPS required. `POST /documents` uploads PDFs to `uploads/` and records each as `uploaded`.
- **ECR and OIDC:** CI pushes images to ECR, logging in with GitHub's short-lived token. No AWS keys are stored in GitHub.
- **EC2:** a `t4g.small` running the same `docker compose` stack as the Mac, with an IAM role instead of keys, IMDSv2 only, and an Elastic IP.
- **RDS:** a `db.t4g.micro` PostgreSQL 16 with no public address. Its security group lets in only the server's. The API connects with TLS and checks the certificate (`sslmode=verify-full`).
- **Parameter Store:** every secret is a SecureString under `/support-ai`, loaded by `app/config.py` at start. The server's `.env` holds none.
- **HTTPS:** Caddy, on the server's network, gets and renews a Let's Encrypt certificate. The Twilio webhook now points at it.
- **Lambda:** `lambda/extract_text.py` runs for each new `uploads/*.pdf`. It saves the text to `text/` and calls `POST /webhooks/document-processed` with a shared secret.
- **Deploys:** CI's `deploy` job runs a fixed Systems Manager document on the server, which moves it to that commit's code and image.

## Key decisions, and why

- **Graviton (ARM) everywhere.** The Mac, CI's runner, EC2, RDS, and Lambda are all ARM, so one image runs everywhere without emulation, and Graviton costs less.
- **One server, one zone.** About $31 a month in all. The single points of failure are written down in `docs/system-design/11-aws-setup.md`, with what would remove each one.
- **Roles, not keys.** The server, the Lambda, and CI each get their own role with only what they use, and none of them stores a key.
- **Parameter Store, not Secrets Manager.** Free, and this project doesn't rotate secrets automatically. A managed RDS password would rotate weekly and break the stored URL.
- **Caddy on the host network.** Its connections to the API arrive from the compose network's gateway, the one address the API already trusted for forwarded headers. So the rate limit and the Twilio signature check worked with no API change.
- **A Lambda for text extraction.** It's short, occasional work that shouldn't hold an API worker. S3's trigger starts it, and Lambda's retries cover the moment the PDF lands before its row is saved.
- **Systems Manager instead of SSH for deploys.** No port opened to GitHub's runners and no SSH key in GitHub. CI can run only the one deploy document, whose only input must be a 40-character commit id.

## How to run and demo it

- Open `https://support.mysolospark.com/docs`, log in with **Authorize**, and upload a PDF with `POST /documents`. Within a few seconds the document is `processed`: `aws s3 ls s3://support-ai-docs-phanindra02/text/` shows its text, and `aws logs tail /aws/lambda/support-ai-extract-text --region us-east-2` shows the run.
- Send a WhatsApp message to the Twilio sandbox: the reply has a ticket number, and the ticket is on RDS.
- Merge a pull request: the Actions run's `deploy` job shows the server pulling `sha-<commit>`, then checks the site.
- `infra/aws/README.md` has every create, check, and change command. Measured numbers are in `docs/results.md`.

## What an interviewer might ask

- **Why not put everything in one public subnet with SSH open?** The database has no public address, and only the server's security group may reach it. SSH is open only to one address, and deploys don't use it at all.
- **How does the API get its database password?** From Parameter Store at start, with the instance role. Decrypting uses the AWS-managed key, through Parameter Store only.
- **What happens if the server dies?** Everything stops. The data survives in RDS and S3. A new instance runs the same compose stack, and the Elastic IP moves to it. Removing that single point of failure is Stage 8.
- **How do you stop the Lambda from triggering itself?** The trigger matches only `uploads/*.pdf`, and the text goes to `text/`. A test checks that the text key can never match the trigger.
- **What if S3 delivers the same event twice?** Saving the text again writes the same key, and marking a processed document again changes nothing.
- **Why check a webhook secret with `hmac.compare_digest`?** A plain `==` returns sooner the earlier the strings differ, which can leak the secret through timing. It's given bytes, so a non-ASCII header can't cause a 500.
- **How would you roll back?** Run the deploy document with the earlier commit's id: the image goes back to `sha-<that commit>` (`docs/rollback.md`).
- **What does this cost?** About $31 a month: the server about $17.50, the database about $14, and the rest is free or nearly so.
