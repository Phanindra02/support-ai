# Containers, virtual machines, and config in the environment

Stage 4 packages the API as a container image, and runs the whole stack (API, Postgres, Redis, n8n) as containers with one command. These notes explain what a container is, how it differs from a virtual machine, and why the app reads its settings from environment variables.

## Containers vs virtual machines

Both let one machine run many isolated programs. They isolate at different levels.

**A virtual machine** runs a whole computer in software. A hypervisor (KVM on Linux, Hyper-V on Windows, Apple's Virtualization framework on a Mac) gives each VM virtual hardware, and each VM boots its own operating system with its own kernel.

**A container** is an ordinary process on the host, with the host's kernel. The kernel just shows it less:

- **Namespaces** give it its own view of process ids, network interfaces, mounted files, and hostname. Inside, the API is process 1 and sees only its own files.
- **cgroups** cap how much CPU and memory it can use.
- **A layered filesystem** (overlayfs) stacks the image's read-only layers, with a thin writable layer on top for each container.

| | Virtual machine | Container |
| --- | --- | --- |
| Kernel | Its own | Shared with the host |
| Starts in | Tens of seconds (it boots an OS) | Under a second to a few seconds |
| Size | Gigabytes (a whole OS) | Megabytes (an app and its libraries) |
| Isolation | Strong: escaping means breaking the hypervisor | Weaker: a kernel bug can let a process escape |
| Runs | Any OS the hardware supports | Programs built for the host's kernel |

In this project, the API container answers its first request about one second after `docker run`. The image is 92 MB compressed (see [results](../results.md#api-image-size-and-build-time-stage-4)).

**The two are often combined.** Cloud servers are VMs, and containers run inside them. AWS Lambda and Fargate run each customer's code in a Firecracker microVM, which starts in a fraction of a second but keeps a separate kernel. That gives VM-level isolation at close to container speed.

**On a Mac, Docker runs a Linux VM.** Containers need a Linux kernel, and macOS doesn't have one, so Docker Desktop starts a small Linux VM (here with 14 CPUs and 8 GB) and runs every container inside it. Two things in this project come from that:

- Requests from the Mac to a published port arrive inside the container from the Docker network's gateway, not from 127.0.0.1. That's why the API is told to trust ngrok's forwarded headers from `172.28.0.1` (see `docker-compose.yml`).
- The VM is ARM, like the M4 Pro, so images built on the Mac are `linux/arm64`. AWS Graviton servers are ARM too, so the same image runs there as it is.

## Images and layers

**An image** is a stack of read-only layers plus some settings, such as the command to run and the user to run it as. **A container** is a running instance of an image. Many containers can share one image.

Each Dockerfile instruction that changes files adds a layer. Docker caches layers, and reuses one if its instruction and its inputs haven't changed. Once one layer changes, every layer after it is rebuilt. So the order of instructions matters:

1. Copy `pyproject.toml` and `uv.lock`, then install the dependencies. This layer only changes when the dependencies do.
2. Copy the code. This changes on almost every commit, but it's small.

With that order, changing one line of app code rebuilt the image in 0.6 s. With the cache thrown away, a rebuild took 8 to 15 s.

**Multi-stage builds** keep build tools out of the final image. The first stage installs the dependencies with uv. The second starts again from a clean `python:3.14-slim` and copies in only the finished virtual environment, so uv and its cache never reach the server.

**Choosing a base image.** `python:3.14` is about 1 GB, with compilers and many libraries the app never uses. `python:3.14-slim` is Debian with just enough to run Python. Alpine is smaller still, but it uses musl instead of glibc, so many Python wheels (psycopg, uvloop, argon2) don't install on it without compiling. Slim is the usual choice.

### What makes an image safe to run

- **A user other than root.** The API runs as user 10001, and its code belongs to root, so a bug in the app can't change the code or install anything. The Dockerfile names the user by number so that Kubernetes can check it isn't root (Stage 8).
- **Nothing secret inside.** `.dockerignore` starts by leaving out every file, then lists the few the build needs. `.env` never reaches the build, so no image holds a password, and the same image can be pushed to a registry safely.
- **Pinned versions.** uv is pinned in the Dockerfile, `uv.lock` pins every package, and `uv sync --locked` fails rather than quietly installing something different.
- **Fixed file permissions.** `COPY --chmod` sets the permissions, so the image is the same whichever machine builds it. Without it, the first build on the Mac made an image whose app user couldn't read its own code, because the Mac's files were only readable by their owner.

## Config belongs in the environment

The [twelve-factor app](https://12factor.net/config) defines config as everything that changes between deploys: database URLs, credentials, hostnames, limits. Code is everything that stays the same. Config should come from **environment variables**, not from files committed with the code.

Why:

- **One image, many places.** The image built in CI should run unchanged on the Mac, in CI, and on AWS. Only the environment around it changes. If the database URL were baked in, every environment would need its own build, and what was tested wouldn't be what runs.
- **Secrets stay out of git.** The test from twelve-factor: could the repo be made public tomorrow without leaking a credential? This repo is public, so that test isn't hypothetical.
- **Changing a setting needs no rebuild.** Changing the rate limit means restarting with a new value, not building a new image.
- **Every platform supports them.** Docker, compose, GitHub Actions, Kubernetes, and Lambda can all set environment variables.

### How this project does it

All settings are read in one place, `app/config.py`, with pydantic-settings. It reads environment variables first, then the git-ignored `.env`, and refuses to start if a required one is missing or too weak. A test fails if any other file reads `os.environ`.

| Where it runs | Where the settings come from |
| --- | --- |
| The Mac | `.env` |
| docker compose | `.env`, passed in by `env_file`. Compose then overrides the hosts, so `DATABASE_URL` points at the `postgres` container. |
| GitHub Actions | The workflow's `env`, plus a fresh random `JWT_SECRET_KEY` for each run |
| AWS (Stage 5) | Parameter Store, loaded when the app starts |

`.env.example` names every setting and leaves every secret empty. Tests check both: that it names exactly the app's settings plus the variables compose reads, and that the four secrets in it are blank.

### Where environment variables fall short

They're simple, but anything that can see the process can see them: `docker inspect`, `/proc/<pid>/environ` on Linux, child processes, and crash reports that dump the environment. The common steps up:

- **Secrets as files.** Docker and Kubernetes secrets are mounted as files that only the app can read, and don't appear in `docker inspect`.
- **A secret manager.** AWS Secrets Manager or Parameter Store (Stage 5) keeps the values encrypted, records who read them, and can rotate them. The app fetches them at startup, using the server's IAM role, so no key is stored on the server at all.

## Interview questions

- **What's the difference between a container and a VM?** Shared kernel vs own kernel. Namespaces and cgroups. The trade between speed and isolation.
- **Why is my image so big, and how would you shrink it?** Slim base, multi-stage build, no dev dependencies, `.dockerignore`. Here, the Twilio SDK alone is 51 MB of the 216 MB of dependencies.
- **Why copy the lock file before the code?** Layer caching: dependencies change rarely, code changes often.
- **Why not run as root in a container?** A process that escapes the container as root is root on the host. Non-root plus read-only code limits the damage of an app bug.
- **Why does it work on my Mac but not on the server?** CPU architecture (ARM vs x86), the kernel, file permissions, and settings that came from a local `.env`. Building the image in CI, from a clean checkout, catches most of these.
- **Why environment variables for config? What's wrong with them for secrets?** One image everywhere, and no secrets in git. But they're visible to anything that can inspect the process, so use mounted files or a secret manager for anything sensitive.
- **How do containers on the same compose network find each other?** By service name. Compose runs a DNS server that resolves `postgres` to that container's address on the network.
