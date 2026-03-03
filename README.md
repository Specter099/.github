# Specter099 Shared GitHub Actions

Reusable workflows and composite actions for CDK projects.

## Quick Reference

| Name | Type | Use When |
|------|------|----------|
| `cdk-review` | Workflow | PR check for a CDK-only project |
| `cdk-deploy` | Workflow | Deploy CDK-only project to prod |
| `static-site-review` | Workflow | PR check for frontend + CDK project |
| `static-site-deploy` | Workflow | Deploy frontend + CDK project to prod |
| `repo-backup` | Workflow | Back up repo zip to S3 |
| `python-ci` | Workflow | PR check for any pure Python project |
| `setup-cdk` | Action | Composite action — install Python/Node/CDK |

> **Required secret:** All workflows assume `AWS_ROLE_ARN` is set on the calling repo's `production` environment (or the environment passed via `environment` input).

---

## CDK Workflows

### `cdk-review`

Lints, unit tests, and dependency-audits a CDK Python project, then runs `cdk synth` + `cdk diff` and posts the diff as a PR comment (updates in place on re-run).

**Inputs**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `aws-region` | no | `us-east-1` | AWS region |
| `cdk-version` | no | `2.1106.1` | CDK CLI version |
| `smoke-test-url` | no | `""` | Unused — accepted for interface parity with deploy |

**Usage**

```yaml
jobs:
  review:
    uses: Specter099/.github/.github/workflows/cdk-review.yml@main
    secrets: inherit
    with:
      aws-region: us-east-1          # optional
      cdk-version: "2.1106.1"        # optional
```

---

### `cdk-deploy`

Deploys a CDK Python project to the `production` environment with `--require-approval never`. Uploads `outputs.json` as an artifact and writes stack outputs to the job summary. Optionally curls a URL for a smoke test.

**Inputs**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `aws-region` | no | `us-east-1` | AWS region |
| `cdk-version` | no | `2.1106.1` | CDK CLI version |
| `smoke-test-url` | no | `""` | URL to curl after deploy |

**Usage**

```yaml
jobs:
  deploy:
    uses: Specter099/.github/.github/workflows/cdk-deploy.yml@main
    secrets: inherit
    with:
      smoke-test-url: https://example.com   # optional
```

---

## Static Site Workflows

### `static-site-review`

PR check for a monorepo with a frontend (npm) and a CDK infra directory. Lints and tests both, builds the frontend, then synths and diffs CDK and posts the diff as a PR comment.

**Inputs**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `frontend-dir` | **yes** | — | Path to dir containing `package.json` |
| `infra-dir` | **yes** | — | Path to dir containing `app.py` |
| `aws-region` | no | `us-east-1` | AWS region |
| `cdk-version` | no | `2.1106.1` | CDK CLI version |
| `skip-frontend-tests` | no | `false` | Skip `npm run test` (for repos without tests) |

**Usage**

```yaml
jobs:
  review:
    uses: Specter099/.github/.github/workflows/static-site-review.yml@main
    secrets: inherit
    with:
      frontend-dir: frontend
      infra-dir: infra
      skip-frontend-tests: false     # optional
```

---

### `static-site-deploy`

Builds the frontend with `npm run build`, then deploys CDK to the `production` environment. Uploads stack outputs as an artifact and optionally runs a smoke test.

**Inputs**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `frontend-dir` | **yes** | — | Path to dir containing `package.json` |
| `infra-dir` | **yes** | — | Path to dir containing `app.py` |
| `aws-region` | no | `us-east-1` | AWS region |
| `cdk-version` | no | `2.1106.1` | CDK CLI version |
| `smoke-test-url` | no | `""` | URL to curl after deploy |

**Usage**

```yaml
jobs:
  deploy:
    uses: Specter099/.github/.github/workflows/static-site-deploy.yml@main
    secrets: inherit
    with:
      frontend-dir: frontend
      infra-dir: infra
      smoke-test-url: https://example.com   # optional
```

---

## Utilities

### `repo-backup`

Archives the repo at HEAD with `git archive`, uploads a timestamped zip (`<repo>-YYYY-MM-DD-<sha7>.zip`) to S3, and writes backup details to the job summary. Supports `workflow_call` and `workflow_dispatch`.

**Inputs**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `s3-bucket` | **yes** | — | S3 bucket name |
| `s3-prefix` | no | repo name | Key prefix (folder) within the bucket |
| `aws-region` | no | `us-east-1` | AWS region of the bucket |
| `environment` | no | `production` | GitHub environment with `AWS_ROLE_ARN` secret |

**Usage**

```yaml
jobs:
  backup:
    uses: Specter099/.github/.github/workflows/repo-backup.yml@main
    secrets: inherit
    with:
      s3-bucket: my-backups-bucket
      s3-prefix: my-repo              # optional, defaults to repo name
```

---

## Python CI

### `python-ci`

Lints, format-checks, and secret-scans a pure Python project, then runs pytest. No AWS credentials required.

> **Requires:** `ruff` and `pytest` must be present in the caller's requirements file.

**Inputs**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `python-version` | no | `"3.12"` | Python version |
| `requirements-path` | no | `"requirements-dev.txt"` | Path to dev requirements file |
| `tests-dir` | no | `"tests/"` | Directory passed to pytest |

**Usage**

```yaml
jobs:
  ci:
    uses: Specter099/.github/.github/workflows/python-ci.yml@main
    with:
      python-version: "3.12"                   # optional
      requirements-path: requirements-dev.txt  # optional
      tests-dir: tests/                        # optional
```

> **Note:** gitleaks scans the full git history. `GITHUB_TOKEN` is injected automatically by GitHub Actions — no secrets configuration needed.

---

## Composite Actions

### `setup-cdk`

Installs Python 3.12, Node 22, a pinned CDK CLI version globally, and Python dependencies from a requirements file. Used internally by all workflows above; can also be referenced directly.

**Inputs**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `cdk-version` | no | `2.1106.1` | CDK CLI npm version |
| `requirements-path` | no | `requirements.txt` | Path to `requirements.txt` relative to repo root |

**Usage**

```yaml
- uses: Specter099/.github/.github/actions/setup-cdk@main
  with:
    cdk-version: "2.1106.1"
    requirements-path: infra/requirements.txt
```
