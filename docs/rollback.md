# Rolling back a bad release

What to do when a release breaks something. The ideas behind these steps are in [deployment strategies](system-design/08-deployment-strategies.md).

## First: roll back, or fix forward?

- **Roll back** when users are affected now and the cause isn't obvious. Get back to the last good version first, then investigate.
- **Fix forward** only when the fix is small, clear, and faster than a rollback.

## 1. Check whether the release changed the database

Look for new files in `alembic/versions/` between the good commit and the bad one:

```bash
git diff --name-only <good-commit> <bad-commit> -- alembic/versions
```

- **No new migration:** go to step 2.
- **A migration the old code can live with,** such as a new table, column, or index: keep it. Roll back the code only, and leave the migration file in place. The old code ignores what it doesn't know about.
- **A migration the old code can't run against,** such as a renamed or dropped column: undo it first, while the bad commit is still checked out, because that's the only version that knows how. Read its `downgrade()`, and back the database up before running it, because a downgrade that drops a column loses whatever was written there since:

  ```bash
  docker compose exec postgres pg_dump -U support support > backup.sql
  docker compose run --rm migrate alembic downgrade -1    # one migration back
  ```

Order matters. If the code goes back first, its `migrate` container finds the database at a revision it has never heard of. `alembic upgrade head` then fails with "Can't locate revision", and the API never starts, because it waits for `migrate` to succeed.

## 2. Roll back the code

Revert the bad commit rather than resetting `main`. That leaves a record in the history, and nothing has to be force-pushed:

```bash
git revert <bad-commit>        # a new commit that undoes it
git push                       # CI lints, tests, and builds it like any other change
```

If the bad commit added a migration the old code can live with, keep the migration file in the revert: `git checkout <bad-commit> -- alembic/versions/<file>`, then amend the revert commit.

Then run the reverted code. With docker compose:

```bash
docker compose up --build -d   # rebuild the image and replace the API
```

To try an older version without changing `main`, check out its commit and run the same command. Afterwards, `git switch main` and run it again to come back.

## 3. Roll back by image tag (from Stage 5)

From Stage 5, CI builds each image once and the server pulls it by tag, so a rollback won't rebuild anything. Every image will carry the commit it was built from (`sha-<commit>`). To roll back, point the server at the last good tag and restart. That takes seconds, and the image is exactly the one that already ran. Step 1 still applies first.

## 4. The n8n workflow

The workflow lives in git, so an older version can be put back:

```bash
git show <good-commit>:infra/n8n/urgent-ticket.json > urgent-ticket-good.json
```

Import that file in n8n (the workflow's `...` menu, then Import from File), check both nodes still use the right credentials, and publish.

## 5. Check that it worked

```bash
docker compose ps              # every service up, migrate exited 0
docker compose logs --tail 50 api
curl http://127.0.0.1:8000/    # {"message": "Support API is running"}
```

Then try what broke: log in at `/docs`, send a WhatsApp message, or mark a ticket urgent. Afterwards, write down what went wrong and what would have caught it sooner, such as a test, a health check, or a canary.
