# Specter099 Shared GitHub Actions

Reusable workflows and composite actions for AWS CDK projects. This repo must be **public** for other repos to reference the workflows and actions.

## Repository Structure

```
.github/
  actions/
    setup-cdk/          # Composite action — shared toolchain setup
      action.yml
  workflows/
    cdk-review.yml      # Reusable workflow — PR checks for CDK-only projects
    cdk-deploy.yml      # Reusable workflow — deploy for CDK-only projects
    static-site-review.yml  # Reusable workflow — PR checks for React + CDK projects
    static-site-deploy.yml  # Reusable workflow — deploy for React + CDK projects
    repo-backup.yml     # Reusable workflow — archive repo to S3
    backup.yml          # Standalone workflow — backs up this repo to S3
```

## Composite Action

### `setup-cdk`

Installs Python 3.12, Node 22, a pinned AWS CDK CLI version, and Python dependencies. Used as a shared first step by all the reusable workflows.

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `cdk-version` | No | `2.1106.1` | CDK CLI npm version to install |
| `requirements-path` | No | `requirements.txt` | Path to `requirements.txt` relative to the repo root |

**Usage:**
```yaml
- name: Setup CDK
  uses: Specter099/.github/.github/actions/setup-cdk@main
  with:
    cdk-version: "2.1106.1"
    requirements-path: infra/requirements.txt
```

## Reusable Workflows

All reusable workflows are triggered via `workflow_call`. Calling repos use thin wrapper workflows that pass inputs and inherit secrets.

### `cdk-review.yml` — CDK PR Review

Runs on pull requests for CDK-only Python projects (no frontend). Performs linting, testing, dependency auditing, CDK synth/diff, and posts the diff as a PR comment.

**Steps:** Checkout > Setup CDK > Install dev deps > Ruff lint > Pytest > pip-audit > AWS credentials > CDK Synth > CDK Nag (informational) > CDK Diff > Comment diff on PR

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `aws-region` | No | `us-east-1` | AWS region |
| `cdk-version` | No | `2.1106.1` | CDK CLI version |
| `smoke-test-url` | No | `""` | Unused — accepted for interface consistency |

**Caller example:**
```yaml
name: Review
on:
  pull_request:
    branches: [main]

jobs:
  review:
    uses: Specter099/.github/.github/workflows/cdk-review.yml@main
    secrets: inherit
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    with:
      aws-region: us-east-1
```

### `cdk-deploy.yml` — CDK Deploy

Runs on push to main for CDK-only Python projects. Deploys the stack, uploads `outputs.json` as an artifact, writes outputs to the job summary, and optionally runs a smoke test.

**Steps:** Checkout > Setup CDK > AWS credentials > CDK Deploy > Upload outputs artifact > Write outputs to summary > Smoke test (optional)

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `aws-region` | No | `us-east-1` | AWS region |
| `cdk-version` | No | `2.1106.1` | CDK CLI version |
| `smoke-test-url` | No | `""` | URL to curl after deploy; skipped if empty |

**Caller example:**
```yaml
name: Deploy
on:
  push:
    branches: [main]

concurrency:
  group: deploy
  cancel-in-progress: false

jobs:
  deploy:
    uses: Specter099/.github/.github/workflows/cdk-deploy.yml@main
    secrets: inherit
    permissions:
      id-token: write
      contents: read
    with:
      smoke-test-url: "https://example.com"
```

### `static-site-review.yml` — Static Site PR Review

Runs on pull requests for projects with a **React/Vite frontend + CDK Python infrastructure** in separate directories. Lints and tests both the frontend and infra, then runs CDK synth/diff and posts the diff as a PR comment.

**Steps:** Checkout > Setup CDK > Install frontend deps (`npm ci`) > ESLint > Vitest (optional) > Build frontend > Install infra dev deps > Pytest > pip-audit > AWS credentials > CDK Synth > CDK Diff > Comment diff on PR

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `frontend-dir` | **Yes** | — | Path to the frontend directory (contains `package.json`) |
| `infra-dir` | **Yes** | — | Path to the CDK infra directory (contains `app.py`) |
| `aws-region` | No | `us-east-1` | AWS region |
| `cdk-version` | No | `2.1106.1` | CDK CLI version |
| `smoke-test-url` | No | `""` | Unused — accepted for interface consistency |
| `skip-frontend-tests` | No | `false` | Set `true` to skip the Vitest step |

**Caller example:**
```yaml
name: Review
on:
  pull_request:
    branches: [main]

jobs:
  review:
    uses: Specter099/.github/.github/workflows/static-site-review.yml@main
    secrets: inherit
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    with:
      frontend-dir: scissors-hair-barber
      infra-dir: infra
```

### `static-site-deploy.yml` — Static Site Deploy

Runs on push to main for React/Vite + CDK projects. Builds the frontend, deploys via CDK, uploads stack outputs, and optionally runs a smoke test.

**Steps:** Checkout > Setup CDK > Install frontend deps > Build frontend > AWS credentials > CDK Deploy > Upload outputs artifact > Write outputs to summary > Smoke test (optional)

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `frontend-dir` | **Yes** | — | Path to the frontend directory |
| `infra-dir` | **Yes** | — | Path to the CDK infra directory |
| `aws-region` | No | `us-east-1` | AWS region |
| `cdk-version` | No | `2.1106.1` | CDK CLI version |
| `smoke-test-url` | No | `""` | URL to curl after deploy; skipped if empty |

**Caller example:**
```yaml
name: Deploy
on:
  push:
    branches: [main]

concurrency:
  group: deploy
  cancel-in-progress: false

jobs:
  deploy:
    uses: Specter099/.github/.github/workflows/static-site-deploy.yml@main
    secrets: inherit
    permissions:
      id-token: write
      contents: read
    with:
      frontend-dir: scissors-hair-barber
      infra-dir: infra
      smoke-test-url: "https://scissorshairandbarber.com"
```

### `repo-backup.yml` — Repo Backup to S3

Archives the full git history as a zip and uploads it to an S3 bucket. Can be called from other repos as a reusable workflow or triggered manually via `workflow_dispatch`.

**Steps:** Checkout (full history) > Resolve filename > Create zip archive > AWS credentials > Upload to S3 > Write job summary

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `s3-bucket` | **Yes** | — | S3 bucket name |
| `s3-prefix` | No | repo name | Key prefix (folder) within the bucket |
| `aws-region` | No | `us-east-1` | AWS region of the S3 bucket |
| `environment` | No | `production` | GitHub environment whose secrets contain `AWS_ROLE_ARN` |

**Caller example:**
```yaml
name: Weekly Backup
on:
  schedule:
    - cron: "0 2 * * 0"

jobs:
  backup:
    uses: Specter099/.github/.github/workflows/repo-backup.yml@main
    secrets: inherit
    with:
      s3-bucket: my-backup-bucket
```

### `backup.yml` — This Repo's Backup

A standalone (non-reusable) workflow that backs up this `.github` repo itself to S3. Runs weekly on Sunday at 02:00 UTC and can be triggered manually.

**Required configuration:**
- **Secret:** `AWS_ROLE_ARN` — IAM role ARN for OIDC authentication (on the `production` environment)
- **Variable:** `BACKUP_S3_BUCKET` — S3 bucket name (on the `production` environment)

## Prerequisites for Calling Repos

### Secrets and Variables

All reusable workflows authenticate to AWS via OIDC (no long-lived keys). Each calling repo needs:

| Name | Type | Where to Set | Description |
|------|------|-------------|-------------|
| `AWS_ROLE_ARN` | Secret | Environment (`production`) | Full IAM role ARN (e.g. `arn:aws:iam::123456789012:role/GitHubActionsRole`) |

### IAM Role Trust Policy

The IAM role's trust policy must allow the calling repo. Use a wildcard `sub` to cover both main branch pushes and PR workflows:

```json
{
  "Condition": {
    "StringLike": {
      "token.actions.githubusercontent.com:sub": "repo:Specter099/<repo-name>:*"
    }
  }
}
```

### GitHub Environment

Deploy workflows reference the `production` environment. Create it in the calling repo under **Settings > Environments > New environment**. Without it, deploy jobs will queue indefinitely.

### Expected Project Layout

**CDK-only projects** (`cdk-review` / `cdk-deploy`):
```
repo-root/
  requirements.txt
  requirements-dev.txt
  app.py
  tests/
```

**Static site projects** (`static-site-review` / `static-site-deploy`):
```
repo-root/
  <frontend-dir>/
    package.json
  <infra-dir>/
    app.py
    requirements.txt
    requirements-dev.txt
    tests/
```

`requirements-dev.txt` should include `pytest` and `pip-audit` (used by the review workflows).
