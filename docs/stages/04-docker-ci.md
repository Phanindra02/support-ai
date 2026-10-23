# Stage 4: Docker and CI/CD

Done when: one command starts the whole stack in containers, and GitHub Actions tests every push and publishes an image.

**Status: done.**

- **One command starts the stack:** `docker compose up --build -d` starts the API, Postgres, Redis, and n8n, and runs the migrations first. From nothing, it was up in 32 s.
- **Every push is tested:** each push, to any branch, runs `lint`, `test` (against Postgres and Redis containers), and `image`. A whole run takes about 2 minutes.
- **An image is published:** pushes to `main` publish `ghcr.io/phanindra02/support-ai-api`, built for ARM. The first one came from run 39691944975, and anyone can pull it.
- **`main` is protected:** a change can only merge through a pull request whose three checks pass, and that includes the repo's owner.
- **Tests:** 404 pass.

## What was built

- **The API's image** (`infra/api.Dockerfile`). A two-stage build on `python:3.14-slim`. The first stage installs the dependencies with uv, and the second copies in only the finished virtual environment and the code. It runs as user 10001, not root, and is 92 MB gzipped.
- **`.dockerignore`**, an allowlist. It starts with `*` and names the few files a build may use, so `.env`, `.git`, and `.venv` can never reach an image.
- **`docker-compose.yml`**, at the repo root. It runs five containers:
  - `api`
  - `migrate`, which runs `alembic upgrade head` and exits
  - `postgres:16`
  - `redis:8`
  - `n8n` 2.35.7
  
  Postgres and n8n keep their data in named volumes.
- **Settings stay in `.env`.** Compose reads it, and only `POSTGRES_PASSWORD` is new. Tests check three things: `.env.example` leaves every secret empty, `.env` is git-ignored, and nothing outside `app/config.py` reads the environment.
- **Ruff.** It's a dev dependency, so the Mac and CI run the same version. The 54 findings were fixed, and new Alembic migrations are linted as they're generated.
- **The CI workflow** (`.github/workflows/ci.yml`), with three jobs:
  - `lint` runs `ruff check`.
  - `test` runs pytest against `postgres:16` and `redis:8` service containers, the same images as compose.
  - `image` runs once both pass, on an ARM runner. It builds the image and checks that it starts and isn't running as root. On `main`, it pushes the image to GitHub Container Registry, tagged `sha-<commit>` and `latest`.
- **Branch protection for `main`**, kept in `infra/github/main-protection.json` and applied with `gh api`. `infra/github/README.md` explains each rule.
- **Tests for the infrastructure files.** `tests/test_docker.py`, `tests/test_compose.py`, and `tests/test_ci.py` read the Dockerfile, the compose file, the workflow, and the protection rules, and check the rules below. The Dockerfile, workflow, and protection tests were each seen to fail when their rule was broken.
- **Docs:**
  - Notes on [containers and config](../system-design/07-containers-and-config.md): containers vs VMs, layers, multi-stage builds, and twelve-factor config.
  - Notes on [deployment strategies](../system-design/08-deployment-strategies.md): recreate, rolling, blue-green, and canary, plus expand-and-contract migrations.
  - [How to roll back a bad release](../rollback.md).
  - README sections on Docker, CI, and changing `main`.
  - Image size, build times, and CI run times in [results.md](../results.md).

## Key decisions and why

**A multi-stage build, with the lock file copied before the code.** The dependencies get a layer of their own, built from `pyproject.toml` and `uv.lock` alone. A code change reuses that layer, so a rebuild on the Mac takes 0.6 s instead of 8 to 15 s. The second stage starts clean, so uv and its cache never reach the server.

**`python:3.14-slim`, not Alpine.** Alpine uses musl instead of glibc, so wheels like psycopg's and argon2's would have to be compiled. Slim is Debian with just enough to run Python.

**Non-root, with the code owned by root.** If the app has a bug, it can't change its own code or take over the container. The user is set by number (10001), so Kubernetes can check it isn't root in Stage 8. `COPY --chmod` fixes the permissions, because COPY otherwise keeps the build machine's. On this Mac, that made the first image's code unreadable to the app user.

**An allowlist `.dockerignore`.** With a blocklist, a new secret file is included until someone remembers to add it. With an allowlist, a new file stays out until someone lists it.

**Compose at the repo root, not in `infra/`.** Compose reads `.env` from the folder the compose file is in, so from `infra/`, every command would need `-f` and `--env-file`. CLAUDE.md's layout now says this.

**Migrations as their own container.** `migrate` runs `alembic upgrade head` and exits, and the API waits for it with `service_completed_successfully`. The API never starts against old tables. Running migrations once, outside the API, also means several API copies (Stage 8) won't race each other to run them.

**Only the API and n8n are published, and only on 127.0.0.1.** Postgres and Redis can't clash with the Mac's own, and nothing outside the Mac can reach them.

**Trust forwarded headers from the gateway only, not `"*"`.** Inside Docker, ngrok's connections arrive from the Docker network's gateway, not 127.0.0.1. So uvicorn ignored `X-Forwarded-Proto` and rebuilt an `http://` URL that failed Twilio's signature check. The common fix, `FORWARDED_ALLOW_IPS="*"`, was tried: any client could then send `X-Forwarded-For: 6.6.6.6` and dodge the per-IP rate limit. Instead, the network has a fixed subnet, and only its gateway, 172.28.0.1, is trusted.

**n8n pinned to an exact version.** n8n upgrades its own database when a new version first starts, and an older version may not open it again. Postgres and Redis are pinned by major version, so they still get bug and security fixes.

**CI tests against real Postgres, not SQLite.** The app uses `SELECT ... FOR UPDATE`, unique constraints, and Postgres types. Tests on SQLite would pass on things that fail in production.

**No secret is written in the workflow.** The test database's password is `support`, because that database only exists for one run. `JWT_SECRET_KEY` is made with `openssl rand` in each run. The workflow's token is read-only, and only the `image` job may write packages.

**Build on an ARM runner.** The Mac and AWS Graviton are both ARM. Building on `ubuntu-24.04-arm` gives an arm64 image without QEMU emulation, which is slow.

**Build on every branch, push only from `main`.** A broken Dockerfile fails on its branch, before it can merge. Only `main` becomes an image a server would run.

**Two tags.** `sha-<commit>` never changes, so a deploy or rollback can name an exact build. `latest` moves to each new build, for convenience.

**A public image, checked first.** The repo is public, so the image is too. Before the first push, every file and layer of a built image was searched for each real `.env` value, and none was found. The Dockerfile has no `ARG`, and tests forbid build args, because anything passed as one is stored in the image.

**Branch protection that holds for admins too.** The rules require:
- **A pull request.** It needs 0 approvals, since nobody can approve their own pull request.
- **All three checks passing**, reported by GitHub Actions (app 15368). Without that, anyone with write access could mark a check as passed through the API.
- **A branch that's up to date with `main`** (`strict`), so two pull requests that each pass alone can't break `main` together.

Without `enforce_admins`, the owner could skip all of it. A test checks that the required checks match the workflow's job names. A renamed job would otherwise leave every pull request waiting forever.

**Ruff for lint only, not formatting.** `ruff format` would rewrite `except (A, B):` as `except A, B:`. That's valid in Python 3.14, but it reads like Python 2's `except A, B:`, which meant something else. UP017 is off because CLAUDE.md asks for `timezone.utc`.

## How to run and demo it

```bash
cp .env.example .env                     # then fill in JWT_SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build -d             # API, Postgres, Redis, n8n; migrations run first
docker compose ps                        # migrate exited 0, the rest running
docker compose exec api python -m scripts.create_agent --email you@example.com --name "Your Name" --admin
```

Stop `uvicorn` and `npx n8n` first, since compose uses the same ports, 8000 and 5678.

1. **One command.** Run `docker compose down`, then `docker compose up -d`. Open http://localhost:8000/docs and log in. The data is still there, because it lives in volumes.
2. **Migrations first.** `docker compose logs migrate` shows Alembic upgrading, and the API only starts after it exits.
3. **Not root.** `docker compose exec api id` prints uid 10001.
4. **No secrets in the image.** `docker run --rm support-ai-api ls -a /app` shows no `.env`.
5. **CI.** Open the Actions tab, or run `gh run list`. Show the three jobs, the Postgres and Redis service containers in the `test` job's log, and the `CACHED` steps in the `image` job's log.
6. **The published image.** Pull it on any ARM machine without logging in:
   ```bash
   docker pull ghcr.io/phanindra02/support-ai-api:latest
   ```
7. **Protected `main`.** Try `git push origin main` with a new commit: GitHub refuses it. Then open a pull request and show the merge button waiting for `lint`, `test`, and `image`.
8. **A rollback.** Walk through [docs/rollback.md](../rollback.md), and explain why the migration check comes first.

## What an interviewer might ask

- **Container or VM, and what is a container really?** A process with namespaces, cgroups, and a layered filesystem, sharing the host's kernel. Why Docker on a Mac still runs a Linux VM.
- **Why copy the lock file before the code?** Layer caching. Show the 0.6 s rebuild against 8 to 15 s.
- **Why does the CI cache save almost nothing?** On GitHub's runner, uv installs every package in under 5 seconds, and moving layers to and from the cache costs more. See [results.md](../results.md).
- **How do you keep secrets out of an image?** The allowlist `.dockerignore`, no build args, and settings given at run time. How you would check a built image.
- **Why not run as root?** What a container escape or an app bug can do as root, and why the code is owned by root.
- **How do you make sure migrations run before the app?** The `migrate` container and `depends_on` conditions. What changes with several replicas.
- **What does `FORWARDED_ALLOW_IPS` do, and why not `"*"`?** The rate-limit bypass, and how a fixed subnet fixes it.
- **How does the CI job get Postgres?** Service containers, health checks, and why not SQLite.
- **What can the workflow's token do?** Least privilege: read-only by default, and `packages: write` only for the `image` job.
- **Why build on ARM?** Emulation cost, and matching Graviton.
- **How would you roll back?** By `sha-` tag once Stage 5 deploys images, and why migrations decide the order. Expand and contract.
- **What does branch protection give a solo developer?** Nothing reaches `main` without passing CI, not even by mistake. Why `strict`, why `app_id`, and why `enforce_admins`.
- **Rolling, blue-green, or canary?** Which one each stage of this project will use, and why the database makes all of them harder.

## Known limitations, for later stages

- **Actions are pinned by version tag, not commit SHA.** Tags are readable and get fixes, but a hijacked tag could run someone else's code with this repo's token. Pinning SHAs, kept current by Dependabot, would close that.
- **The tests run on x86, and the image runs on ARM.** `test` uses `ubuntu-latest`. The `image` job's smoke test proves the ARM image starts, but the full suite never runs on ARM.
- **No scanning.** Nothing checks the image or dependencies for known vulnerabilities. Trivy or Grype in the `image` job, and Dependabot for `uv.lock`, are the usual fixes.
- **No second reviewer.** Pull requests need 0 approvals, because there's one developer. CI is the only gate.
- **Deploys are still manual, and only to the Mac.** Stage 5 deploys the image to EC2 from CI.
- **Formatting isn't enforced.** See the ruff decision above.

## Changes made on the developer's Mac and GitHub

- **Docker Desktop** is installed and runs the `docker` CLI from `~/.docker/bin`. The old Docker links in `/usr/local/bin`, which pointed at a 2022 install on an external drive, are gone.
- **`.env`** has a generated `POSTGRES_PASSWORD`.
- **The day-to-day setup is unchanged.** The WhatsApp and Slack demo still runs on `uvicorn` and `npx n8n`. Switching to compose is optional. To move n8n's workflow and credentials into the container, follow [infra/n8n/README.md](../../infra/n8n/README.md#move-from-npx-n8n-to-docker). The test stacks used while building this stage, with their own ports and volumes, were removed.
- **GitHub Container Registry** has the public package `support-ai-api`, linked to the repo by its labels.
- **`main` is protected**, with the rules in `infra/github/main-protection.json`. Every change, the developer's own included, now goes through a pull request.
