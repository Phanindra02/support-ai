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

## The server (EC2)

One `t4g.small` instance named `support-ai`: 2 Graviton (ARM) vCPUs and 2 GB of memory, running Ubuntu 24.04. It's ARM like the Mac and like CI's runner, so the image CI builds runs on it as is. It runs the same `docker compose` stack as the Mac, except that its database is RDS ([below](#the-database-rds)).

- **IAM role `support-ai-server`** gives the server's programs AWS credentials, through the instance metadata service, so no AWS keys are stored on it. `ec2-permissions.json` allows only what the app does: save uploads under `uploads/` in the docs bucket, pull the API image from ECR, and read the database's URL from Parameter Store. A role reaches an instance through an instance profile of the same name.
- **IMDSv2 only, with a hop limit of 2.** IMDSv2 needs a token from a PUT request first, which blocks the classic attack where a server is tricked into fetching the metadata URL and leaking its credentials. The hop limit of 2 lets a container reach the metadata service: compose's network adds one hop, and at the default of 1 the API couldn't get the role's credentials.
- **`ec2-user-data.sh`** runs once at first boot. It installs Docker with the compose plugin, sets up ECR logins through the instance role, and adds 2 GB of swap.
- **Security group `support-ai-server`** is the firewall. It lets in SSH (port 22) only from the Mac's public IP. The API and n8n listen only on the server's 127.0.0.1, as on the Mac, and nothing else is let in until Caddy serves HTTPS on 80 and 443.
- **SSH key** `~/.ssh/support-ai-ec2` is made on the Mac. Only its public half goes to AWS, as the key pair `support-ai`.
- **Elastic IP:** a fixed public address, so the domain and Twilio's webhook keep working after the instance stops and starts.
- **The disk** is 20 GB of encrypted gp3. **Termination protection** is on, so the instance can't be deleted by mistake.

Cost: the instance $12.26, the disk $1.60, and the public IPv4 address $3.65, about $17.50 a month. The address is billed even while the instance is stopped.

### Create it

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws iam create-role --role-name support-ai-server \
  --description "The EC2 server: saves uploads to S3 and pulls the API image from ECR" \
  --assume-role-policy-document file://infra/aws/ec2-trust-policy.json
aws iam put-role-policy --role-name support-ai-server --policy-name app-access \
  --policy-document "$(sed "s/ACCOUNT_ID/$ACCOUNT_ID/" infra/aws/ec2-permissions.json)"
aws iam create-instance-profile --instance-profile-name support-ai-server
aws iam add-role-to-instance-profile --instance-profile-name support-ai-server \
  --role-name support-ai-server

ssh-keygen -t ed25519 -f ~/.ssh/support-ai-ec2 -C support-ai-ec2
aws ec2 import-key-pair --region us-east-2 --key-name support-ai \
  --public-key-material fileb://$HOME/.ssh/support-ai-ec2.pub

SG_ID=$(aws ec2 create-security-group --region us-east-2 --group-name support-ai-server \
  --description "support-ai EC2 server" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region us-east-2 --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$(curl -s https://checkip.amazonaws.com)/32"

# The newest Ubuntu 24.04 image for ARM, which Canonical publishes in Parameter Store.
AMI_ID=$(aws ssm get-parameter --region us-east-2 \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)
INSTANCE_ID=$(aws ec2 run-instances --region us-east-2 --image-id "$AMI_ID" \
  --instance-type t4g.small --key-name support-ai --security-group-ids "$SG_ID" \
  --iam-instance-profile Name=support-ai-server \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3","Encrypted":true}}]' \
  --user-data file://infra/aws/ec2-user-data.sh --disable-api-termination \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=support-ai}]' \
  'ResourceType=volume,Tags=[{Key=Name,Value=support-ai}]' \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --region us-east-2 --instance-ids "$INSTANCE_ID"

ALLOC_ID=$(aws ec2 allocate-address --region us-east-2 --domain vpc \
  --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=support-ai}]' \
  --query AllocationId --output text)
aws ec2 associate-address --region us-east-2 --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID"
```

A new instance profile can take a few seconds to be usable. If `run-instances` says the profile is invalid, wait a moment and run it again.

### Reach it

```bash
SERVER_IP=$(aws ec2 describe-addresses --region us-east-2 \
  --filters Name=tag:Name,Values=support-ai --query 'Addresses[0].PublicIp' --output text)
ssh -i ~/.ssh/support-ai-ec2 ubuntu@"$SERVER_IP"
```

If SSH stops answering, the Mac's public IP has probably changed (home internet does that). Let the new one in, and remove the old rule in the console:

```bash
SG_ID=$(aws ec2 describe-security-groups --region us-east-2 \
  --group-names support-ai-server --query 'SecurityGroups[0].GroupId' --output text)
aws ec2 authorize-security-group-ingress --region us-east-2 --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$(curl -s https://checkip.amazonaws.com)/32"
```

### Run the stack on it

The same `docker-compose.yml` as on the Mac, except that the API image comes from ECR instead of being built: `API_IMAGE` in the server's `.env` names it. Pulling uses the instance role, through the ECR login that first boot set up.

```bash
# On the server, after ssh:
git clone https://github.com/Phanindra02/support-ai.git
cd support-ai
cp .env.example .env
chmod 600 .env

# The ECR registry's name, from the login first boot set up. New secrets are made here, on
# the server, so they never pass through the Mac.
REGISTRY=$(python3 -c "import json; print(*json.load(open('/home/ubuntu/.docker/config.json'))['credHelpers'])")
sed -i \
  -e "s|^API_IMAGE=.*|API_IMAGE=${REGISTRY}/support-ai-api:latest|" \
  -e "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$(openssl rand -hex 32)|" \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" \
  .env

docker compose pull
docker compose up -d --no-build
docker compose exec api python -m scripts.create_agent --email you@example.com --name "Your Name" --admin
```

`--no-build` makes compose use the pulled image instead of building one on the server. `TWILIO_AUTH_TOKEN` and `N8N_WEBHOOK_SECRET` stay empty, so the WhatsApp webhook answers 503 and alerts are skipped until they're set. Once the database and the secrets below are set up, the app loads every secret from Parameter Store instead, and the server's `.env` holds none of them.

Until Caddy serves HTTPS, nothing but SSH is open, so reach the API and n8n through an SSH tunnel. Then http://localhost:8001/docs on the Mac is the server's API, and http://localhost:5679 its n8n:

```bash
ssh -i ~/.ssh/support-ai-ec2 -N -L 8001:localhost:8000 -L 5679:localhost:5678 ubuntu@"$SERVER_IP"
```

To run a newer image, until CI does it after each merge:

```bash
cd ~/support-ai && git pull && docker compose pull && docker compose up -d --no-build
```

### Move it to RDS

Once the database and the secrets below exist, the server uses RDS instead of its Postgres container. `compose.ec2.yml` holds the server's changes to `docker-compose.yml`. The API and the migrations load `DATABASE_URL` and the other secrets from Parameter Store, and the Postgres container no longer starts. One line in the server's `.env`, `COMPOSE_FILE`, makes every `docker compose` command there read both files.

The copy below also needs the URL, for psql. The app's own settings print it, so it comes from Parameter Store the same way. It's kept in a shell variable and never shown.

`docker compose run` reads whatever input follows it, so when these lines are pasted, or piped in with `ssh server bash -s`, it swallows the lines after it and they never run. `</dev/null` gives it nothing to read.

```bash
# On the server:
cd ~/support-ai && git pull && docker compose pull

echo "COMPOSE_FILE=docker-compose.yml:infra/aws/compose.ec2.yml" >> .env
DATABASE_URL=$(docker compose run --rm --no-deps -T api python -c "from app.config import settings; print(settings.database_url)" </dev/null)

# Stop the API, so nothing changes during the copy. Then make the tables on RDS.
docker compose stop api
docker compose run --rm migrate </dev/null

# Copy the rows from the container to RDS. Only the rows: the migrations made the tables.
# psql wants the same URL without SQLAlchemy's "+psycopg". --single-transaction copies all of
# it or, if anything fails, none of it.
docker exec support-ai-postgres-1 pg_dump -U support -d support --data-only \
    --exclude-table=alembic_version \
  | docker run --rm -i -v "$PWD/infra/aws/rds-ca-us-east-2.pem:/rds.pem:ro" -e PGSSLROOTCERT=/rds.pem \
    postgres:16 psql -v ON_ERROR_STOP=1 --quiet --single-transaction "${DATABASE_URL/+psycopg/}"
unset DATABASE_URL

docker compose up -d --no-build
docker stop support-ai-postgres-1
```

The old container and its volume are kept, as a way back. To go back, delete the `COMPOSE_FILE` line from `.env` and run `docker compose up -d --no-build`, which brings back the data as it was at the move. Once RDS has run for a while without trouble, `docker rm support-ai-postgres-1` and `docker volume rm support-ai_postgres-data` remove them.

To check that the API's connection to RDS is encrypted. It printed `(True, 'TLSv1.3')` after the move:

```bash
docker compose exec -T api python -c "from sqlalchemy import text; from app.database import engine; print(engine.connect().execute(text('select ssl, version from pg_stat_ssl where pid = pg_backend_pid()')).one())" </dev/null
```

## The database (RDS)

One RDS PostgreSQL instance named `support-ai`, which replaces the Postgres container on the server. AWS runs Postgres for us: it installs minor-version fixes, backs the database up every night, and can restore it to any moment of the last 7 days, up to about 5 minutes ago. `rds-instance.json` holds every setting:

- **`db.t4g.micro`**, the smallest instance RDS has: 2 Graviton vCPUs (a burstable share of them) and 1 GB of memory. Plenty for this project's traffic, and the class can be changed later with a few minutes of downtime.
- **PostgreSQL 16**, the same major version as Homebrew's on the Mac, compose's `postgres:16`, and CI's, so the migrations run the same everywhere. `16.15` was the newest 16 that RDS offered, and `AutoMinorVersionUpgrade` installs later 16.x fixes in the Sunday maintenance window. **Extended Support is turned off**, so when 16 reaches the end of standard support (February 2029), AWS warns and then upgrades it, instead of quietly adding a per-hour Extended Support charge.
- **One copy in one availability zone (`MultiAZ: false`).** Multi-AZ keeps a standby copy in a second zone and switches to it within a couple of minutes if the first fails, but it doubles the cost. Without it the database is a single point of failure, recovered from backups. It's in `us-east-2c`, the server's zone, so their traffic doesn't cross zones, which adds latency and is billed per GB.
- **No public address (`PubliclyAccessible: false`).** Its name resolves to a private address (172.31.x.x), which only works inside the VPC, so nothing on the internet can even try to connect.
- **Security group `support-ai-db`** lets in Postgres's port 5432 only from the `support-ai-server` security group, which means only from the EC2 instance. The rule names the server's group rather than its IP address, so it keeps working if the server is replaced. The database never starts connections itself, so its group has no outbound rule.
- **Subnet group `support-ai`** names the subnets RDS may put the database in. RDS wants at least two zones, so it gets the default VPC's three. They are public subnets, which have a route to the internet, but with no public address and that security group, the database can't be reached through it. Private subnets would be one more layer; the default VPC has none, and making them is a step this project skips.
- **20 GB of encrypted gp3 storage**, the smallest gp3 size. Encryption uses the AWS-managed key, which costs nothing. Storage autoscaling stays off, so the bill can't grow by itself.
- **Backups are kept 7 days**, and taken from 08:00 to 08:30 UTC, early morning in Ohio. Maintenance runs on Sundays right after. **Deletion protection** is on, so the instance can't be deleted until it's turned off.
- **Performance Insights and Enhanced Monitoring are off.** The free CloudWatch metrics (CPU, connections, free storage) are enough here.

RDS for PostgreSQL 16 only accepts encrypted connections (`rds.force_ssl` is on by default). The API's URL asks for `sslmode=verify-full`, which also checks the database's certificate against Amazon's RDS certificate bundle, so it can't be tricked into talking to an impostor. `rds-ca-us-east-2.pem` is that bundle, Amazon's root certificates for RDS in `us-east-2`, and the API's image includes it. It came from `https://truststore.pki.rds.amazonaws.com/us-east-2/us-east-2-bundle.pem`, and the SHA-1 fingerprints of its three certificates match what `aws rds describe-certificates` reports. They're valid until 2061 and later.

### The password

The database's password is made in the same commands that create it, and goes straight into Parameter Store as `/support-ai/DATABASE_URL`, the whole URL the API needs. It's a SecureString, encrypted with the AWS-managed `aws/ssm` key, and it's never printed or saved on the Mac. The app loads it, with the other secrets, when it starts ([below](#the-secrets-parameter-store)).

RDS can instead keep the password in Secrets Manager (`--manage-master-user-password`), which changes it every 7 days. That would break the URL stored here each week, and costs $0.40 a month per secret, so this project keeps one place for secrets: Parameter Store.

The API logs in as the master user, `support`. It isn't a true Postgres superuser on RDS, but it can do anything in the `support` database. A separate app user with fewer rights would be safer, and is one more password to manage.

Cost: the instance $11.68 (at $0.016 an hour), the storage $2.30, about $14 a month. Backups up to the database's size, 20 GB, are free. RDS always runs t4g instances in unlimited mode: if the CPU stayed busy above its baseline for hours, the extra would be billed at $0.075 per vCPU-hour, which this project's traffic won't come near.

### Create it

```bash
SERVER_SG_ID=$(aws ec2 describe-security-groups --region us-east-2 \
  --group-names support-ai-server --query 'SecurityGroups[0].GroupId' --output text)
DB_SG_ID=$(aws ec2 create-security-group --region us-east-2 --group-name support-ai-db \
  --description "support-ai RDS database" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region us-east-2 --group-id "$DB_SG_ID" \
  --protocol tcp --port 5432 --source-group "$SERVER_SG_ID"
# New groups allow every outbound connection. The database never makes one.
aws ec2 revoke-security-group-egress --region us-east-2 --group-id "$DB_SG_ID" \
  --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]'

# The default VPC's subnets. The $(...) is left unquoted so it splits into one ID per argument.
aws rds create-db-subnet-group --region us-east-2 --db-subnet-group-name support-ai \
  --db-subnet-group-description "support-ai: the default VPC's subnets" \
  --subnet-ids $(aws ec2 describe-subnets --region us-east-2 \
    --filters Name=default-for-az,Values=true --query 'Subnets[].SubnetId' --output text)

# Letters and digits only, because it goes into a URL. Takes about 10 minutes.
DB_PASSWORD=$(openssl rand -hex 24)
aws rds create-db-instance --region us-east-2 --cli-input-json file://infra/aws/rds-instance.json \
  --vpc-security-group-ids "$DB_SG_ID" --master-user-password "$DB_PASSWORD"
aws rds wait db-instance-available --region us-east-2 --db-instance-identifier support-ai

DB_HOST=$(aws rds describe-db-instances --region us-east-2 --db-instance-identifier support-ai \
  --query 'DBInstances[0].Endpoint.Address' --output text)
aws ssm put-parameter --region us-east-2 --name /support-ai/DATABASE_URL --type SecureString \
  --value "postgresql+psycopg://support:${DB_PASSWORD}@${DB_HOST}:5432/support?sslmode=verify-full"
unset DB_PASSWORD
```

Settings given on the command line are added to the ones in the JSON file, which is why the password and the security group (whose ID differs each time it's made) aren't in it.

If the terminal closes before `put-parameter`, the password is lost, but nothing is broken. Set a new one, then store the URL with `--overwrite`:

```bash
DB_PASSWORD=$(openssl rand -hex 24)
aws rds modify-db-instance --region us-east-2 --db-instance-identifier support-ai \
  --master-user-password "$DB_PASSWORD" --apply-immediately
```

### Check it

```bash
aws rds describe-db-instances --region us-east-2 --db-instance-identifier support-ai \
  --query 'DBInstances[0].[DBInstanceStatus,EngineVersion,AvailabilityZone,PubliclyAccessible,StorageEncrypted]'
aws ssm describe-parameters --region us-east-2 \
  --parameter-filters Key=Name,Values=/support-ai/DATABASE_URL --query 'Parameters[0].Type'

DB_HOST=$(aws rds describe-db-instances --region us-east-2 --db-instance-identifier support-ai \
  --query 'DBInstances[0].Endpoint.Address' --output text)
# From the server, Postgres answers. pg_isready needs no password.
ssh -i ~/.ssh/support-ai-ec2 ubuntu@"$SERVER_IP" docker run --rm postgres:16 pg_isready -h "$DB_HOST"
# From the Mac, the name resolves to a private address the Mac can't reach, so this times out.
nc -z -G 5 "$DB_HOST" 5432
```

To check the certificate without the password, on the server, from `~/support-ai`. With the bundle, psql gets through the TLS check and stops at "no password supplied". With only the public CAs (`sslrootcert=system`), it fails with "certificate verify failed", and by IP address instead of the name, with "does not match host name". Without TLS (`sslmode=disable`), RDS refuses it: "no encryption".

```bash
docker run --rm -v "$PWD/infra/aws/rds-ca-us-east-2.pem:/rds.pem:ro" postgres:16 \
  psql -w "host=$DB_HOST user=support dbname=support sslmode=verify-full sslrootcert=/rds.pem"
```

## The secrets (Parameter Store)

On EC2, every secret the app needs is a SecureString parameter under `/support-ai`, in AWS Systems Manager Parameter Store. None of them is in the server's `.env`, on its disk, or in the image.

| Parameter | What it is | Set by |
| --- | --- | --- |
| `/support-ai/DATABASE_URL` | The RDS URL, with its password | The commands that created the database |
| `/support-ai/JWT_SECRET_KEY` | Signs agents' login tokens | A random value, made below |
| `/support-ai/TWILIO_AUTH_TOKEN` | Checks that WhatsApp webhooks came from Twilio | The developer, copied from the Mac's `.env` |
| `/support-ai/N8N_WEBHOOK_SECRET` | Proves alerts to n8n came from the API | The developer, once the server's n8n has the workflow |

**How the app loads them.** `compose.ec2.yml` sets `PARAMETER_STORE_PATH=/support-ai` for the API and the migrations. When the app starts, `app/config.py` makes one `GetParametersByPath` request (more if there are over 10), with decryption, and the part of each name after the last `/` names the setting it fills. The first place that sets a value wins: a real environment variable, then `.env`, then Parameter Store, then the default in `app/config.py`. So the server's `.env` must leave these four out, or empty (`NAME=` counts as not set). On the Mac and in CI, `PARAMETER_STORE_PATH` is empty, and the app never asks AWS.

The app reads them only when it starts. If AWS can't be reached, the API stops within about a minute with an error, rather than running without its secrets, and Docker starts it again (`restart: unless-stopped`).

**Who can read them.** The server's role may call only `ssm:GetParametersByPath`, on `parameter/support-ai` (`ec2-permissions.json`). IAM checks that against the path the request asks for, so the role can read what's directly under `/support-ai`, and nothing deeper or anywhere else. It can't change or delete a parameter. Decrypting needs no KMS permission: the AWS-managed `aws/ssm` key lets anyone in the account use it, but only through Parameter Store.

Cost: standard parameters, and their default throughput, are free. Secrets Manager would cost $0.40 a month per secret, for automatic rotation that this project doesn't use.

### Create them

```bash
# On the Mac. The server's role, allowed to read the parameters:
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws iam put-role-policy --role-name support-ai-server --policy-name app-access \
  --policy-document "$(sed "s/ACCOUNT_ID/$ACCOUNT_ID/" infra/aws/ec2-permissions.json)"

# A new key for signing login tokens, never shown. Tokens signed with the old one stop working.
aws ssm put-parameter --region us-east-2 --name /support-ai/JWT_SECRET_KEY --type SecureString \
  --value "$(openssl rand -hex 32)"

# The same Twilio account as the Mac, so the same token, read from the Mac's .env.
aws ssm put-parameter --region us-east-2 --name /support-ai/TWILIO_AUTH_TOKEN --type SecureString \
  --value "$(grep '^TWILIO_AUTH_TOKEN=' .env | cut -d= -f2-)"
```

`DATABASE_URL` is already there, from the database's commands. `N8N_WEBHOOK_SECRET` waits until the server's n8n has the workflow, with its own Header Auth value:

```bash
aws ssm put-parameter --region us-east-2 --name /support-ai/N8N_WEBHOOK_SECRET --type SecureString \
  --value "$(openssl rand -hex 32)"
# Then copy it into the Header Auth credential of the server's n8n:
aws ssm get-parameter --region us-east-2 --name /support-ai/N8N_WEBHOOK_SECRET \
  --with-decryption --query Parameter.Value --output text
```

Then, on the server, empty the secrets in `.env`, so Parameter Store's values are used, and restart. `POSTGRES_PASSWORD` stays, because `docker-compose.yml` won't load without it, though the container it was for no longer runs.

```bash
cd ~/support-ai && git pull && docker compose pull
sed -i -E 's/^(DATABASE_URL|JWT_SECRET_KEY|TWILIO_AUTH_TOKEN|N8N_WEBHOOK_SECRET)=.*/\1=/' .env
docker compose up -d --no-build
```

### Change one

Write the new value with `--overwrite`, then restart the API, which reads secrets only when it starts. `read -rs` (zsh, on the Mac) takes the value without showing it or saving it in the shell's history:

```bash
read -rs "VALUE?New value: " && echo
aws ssm put-parameter --region us-east-2 --name /support-ai/NAME --type SecureString --overwrite \
  --value "$VALUE"
unset VALUE
# On the server:
docker compose up -d --no-build --force-recreate api
```

### Check them

```bash
# Names, types, and versions, without the values:
aws ssm describe-parameters --region us-east-2 \
  --parameter-filters Key=Name,Option=BeginsWith,Values=/support-ai/ \
  --query 'Parameters[].[Name,Type,Version]' --output table

# On the server: the container has no secret in its environment, and the app still has them.
docker compose exec -T api sh -c 'env | grep -cE "^(DATABASE_URL|JWT_SECRET_KEY)=."' </dev/null
docker compose exec -T api python -c "from app.config import settings; print(settings.database_url.split('@')[1])" </dev/null
```
