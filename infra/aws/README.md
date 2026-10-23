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
