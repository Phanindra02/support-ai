# Branch protection

`main-protection.json` holds the protection rules for `main`. GitHub keeps them in the repo's settings, not in git, so this file is the written-down copy: it can be reviewed in a pull request, and applied again if the settings are ever lost.

## What each rule does

- **`required_status_checks`**: a pull request can only merge once the `lint`, `test`, and `image` jobs in [ci.yml](../../.github/workflows/ci.yml) pass on its latest commit.
  - `app_id` 15368 is GitHub Actions. Without it, anyone who can write to the repo could mark a check as passed through the API.
  - `strict` means the branch must be up to date with `main`. Otherwise two pull requests could each pass on their own, and break once both are merged.
- **`required_pull_request_reviews`**: every change goes through a pull request, never a direct push. It asks for 0 approvals, since a solo developer can't approve their own pull request.
- **`enforce_admins`**: the rules apply to the repo's owner too, so nothing skips CI.
- **`allow_force_pushes`** and **`allow_deletions`** are off, so `main`'s history can't be rewritten and the branch can't be deleted.

`tests/test_ci.py` checks that the required checks match the job names in `ci.yml`. If a job were renamed, its check would never report under the old name, and every pull request would wait forever.

## Apply it

Run this from the repo root. It needs admin access to the repo.

```bash
gh api -X PUT repos/Phanindra02/support-ai/branches/main/protection --input infra/github/main-protection.json
```

To see what's set now: `gh api repos/Phanindra02/support-ai/branches/main/protection`.
