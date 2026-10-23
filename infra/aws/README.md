# AWS setup

Everything the project creates in AWS, written down so it can be reviewed in a pull request and created again from scratch. Everything lives in `us-east-2` (Ohio), which costs the same as `us-east-1` and is in the developer's time zone.

The commands need the AWS CLI, logged in as the IAM admin user (`aws configure`).

## The help docs bucket

`support-ai-docs-phanindra02` holds the PDFs agents upload, and the text the Lambda pulls out of them. S3 bucket names are global across every AWS account, so the name ends in the GitHub username to keep it unique.

It's private:

- **`docs-bucket-public-access-block.json`** turns on all four Block Public Access settings, so no ACL or bucket policy can ever make an object public, even by mistake. New buckets start this way, and setting it explicitly keeps it written down.
- **`docs-bucket-policy.json`** refuses any request that doesn't use HTTPS, so documents are never sent over the network unencrypted. boto3 already uses HTTPS, so this only matters if something is misconfigured.

What the bucket doesn't need:

- **Encryption at rest** is on by default for every new bucket (SSE-S3, with keys AWS manages). Nothing to set.
- **ACLs** are off by default (Object Ownership is "bucket owner enforced"), so access is decided only by IAM and the bucket policy.
- **Versioning** stays off. Every upload gets its own key, so nothing is ever overwritten, and old versions would only add storage cost.

Cost: S3 Standard is $0.023 per GB-month, so a few hundred PDFs cost well under a cent a month.

`tests/test_aws.py` checks both files, so a change that would loosen them fails CI.

### Create it

```bash
aws s3api create-bucket --bucket support-ai-docs-phanindra02 --region us-east-2 \
  --create-bucket-configuration LocationConstraint=us-east-2
aws s3api put-public-access-block --bucket support-ai-docs-phanindra02 \
  --public-access-block-configuration file://infra/aws/docs-bucket-public-access-block.json
aws s3api put-bucket-policy --bucket support-ai-docs-phanindra02 \
  --policy file://infra/aws/docs-bucket-policy.json
```

`us-east-1` is the only region that rejects `LocationConstraint`, so other regions need it and `us-east-1` must leave it out.

### Check it

```bash
aws s3api get-public-access-block --bucket support-ai-docs-phanindra02
aws s3api get-bucket-policy --bucket support-ai-docs-phanindra02 --query Policy --output text
aws s3api get-bucket-encryption --bucket support-ai-docs-phanindra02
```

## The image registry (ECR)

CI pushes the API's image to `support-ai-api` in ECR, Amazon's container registry. EC2 pulls it from there with its own IAM role, so the server never needs a registry password.

- **`sha-` tags can never be overwritten** (`IMMUTABLE_WITH_EXCLUSION`). `sha-<commit>` always names the exact build of that commit, which is what a rollback relies on ([docs/rollback.md](../../docs/rollback.md)). Only `latest` is excluded, so it can move to each new build.
- **Every push is scanned** for known vulnerabilities in the image's OS packages (basic scanning, which is free). The results are in the ECR console.
- **`ecr-lifecycle-policy.json`** keeps the 10 newest builds and deletes older ones, plus anything left untagged, so storage doesn't grow forever.

Cost: $0.10 per GB-month of stored images. Builds share most of their layers, so 10 of them come to a few hundred MB, about $0.05 a month. Pulling to EC2 in the same region is free.

## How CI logs in to AWS (OIDC)

CI has no AWS keys. GitHub gives each workflow run a short-lived signed token (OpenID Connect) saying which repo and branch it's running for. AWS checks the signature, and if the token matches the role's trust policy, hands back credentials that expire within an hour. Nothing long-lived can leak from the repo or its logs.

- **`github-ci-trust-policy.json`** says who may use the role: only runs for pushes to `main` of this repo. The token's audience must be `sts.amazonaws.com`, so a token GitHub issued for some other service doesn't work here.
  - The repo was created after July 15, 2026, so GitHub's tokens use immutable subjects, which carry the owner and repo ids: `repo:Phanindra02@60551624/support-ai@1424736820:...`. If the repo were deleted and someone made a new one with the same name, its ids would differ, so its runs couldn't use the role. `gh api repos/Phanindra02/support-ai/actions/oidc/customization/sub` shows the prefix.
  - Branches and pull requests get a different subject, so they can build the image but never push it.
- **`github-ci-permissions.json`** says what the role may do: log in to ECR, and push to `support-ai-api` only. `GetAuthorizationToken` has to be `"*"`, because AWS doesn't let it be limited to one repository.

`ACCOUNT_ID` in both files stands for the AWS account number, filled in when they're applied, so the public repo doesn't show it. The workflow reads the role's ARN from the repo variable `AWS_ROLE_ARN`, for the same reason, and masks the account number in its logs.

### Create them

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws ecr create-repository --repository-name support-ai-api --region us-east-2 \
  --image-tag-mutability IMMUTABLE_WITH_EXCLUSION \
  --image-tag-mutability-exclusion-filters filterType=WILDCARD,filter=latest \
  --image-scanning-configuration scanOnPush=true
aws ecr put-lifecycle-policy --repository-name support-ai-api --region us-east-2 \
  --lifecycle-policy-text file://infra/aws/ecr-lifecycle-policy.json

# Once per account: lets IAM trust GitHub's tokens. AWS checks GitHub's certificate itself.
aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
aws iam create-role --role-name support-ai-ci-push-image \
  --description "GitHub Actions on main pushes the API image to ECR" \
  --assume-role-policy-document "$(sed "s/ACCOUNT_ID/$ACCOUNT_ID/" infra/aws/github-ci-trust-policy.json)"
aws iam put-role-policy --role-name support-ai-ci-push-image --policy-name push-api-image \
  --policy-document "$(sed "s/ACCOUNT_ID/$ACCOUNT_ID/" infra/aws/github-ci-permissions.json)"

gh variable set AWS_ROLE_ARN --body "arn:aws:iam::${ACCOUNT_ID}:role/support-ai-ci-push-image"
```

### Check them

```bash
aws ecr describe-repositories --repository-names support-ai-api --region us-east-2
aws ecr list-images --repository-name support-ai-api --region us-east-2
aws iam get-role --role-name support-ai-ci-push-image --query Role.AssumeRolePolicyDocument
```
