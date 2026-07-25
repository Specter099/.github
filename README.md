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

## Local development

Run the gate before committing. It executes the same stages as
`.github/workflows/self-test.yml`, so a green run locally means a green check on
the PR — and it takes about three seconds.

```bash
pip install -r requirements-dev.txt
./scripts/local-ci.sh
```

| Flag | What it does |
|------|--------------|
| *(none)* | yamllint, ruff, pytest, workflow invariants, actionlint; zizmor advisory |
| `--fix` | `ruff format` first, then the gate |
| `--strict` | Ignore the invariants baseline — shows the outstanding work list |
| `--fast` | Skip the slower advisory stages |
| `--cdk-project DIR` | Also run the CD-side checks against a caller repo (below) |
| `--act` | Execute `self-test.yml` locally under [`act`](https://github.com/nektos/act) (needs Docker) |
| `--install-hook` | Install as `.git/hooks/pre-commit` (bypass with `git commit --no-verify`) |

Stages come in three kinds, and the distinction is load-bearing:

- **required** — `yamllint`, `ruff`, `pytest`, workflow invariants, `actionlint`.
  These are what CI runs. If one *cannot* run because its tool is absent, the
  verdict is **INCOMPLETE** (exit 1), never PASS. An earlier version skipped a
  missing `yamllint` and still printed "Safe to commit", which reproduced the
  very local/CI divergence the gate exists to prevent — "I didn't check" is not
  "it's fine".
- **advisory** — `zizmor`, `act`. Report findings, never block.
- **optional** — `cdk`, `aws`. Only used by the CD stages; skipped when absent.

`actionlint` is auto-installed via `go install` at a pinned version when Go is
present; if one is already on `PATH` at a different version, the gate says so,
since two versions report different findings. Use `--fast` to opt out of
`zizmor` and `actionlint` explicitly — an explicit opt-out counts as skipped
rather than missing, so PASS is still reachable.

Python tools are invoked as `python3 -m ruff` / `python3 -m pytest` rather than
via a bare command, and `ruff` is pinned exactly in `requirements-dev.txt`.
Both are load-bearing: a `ruff` shadowed earlier on `PATH` was a different
version with a different default rule set than the one `pip` installed, which is
the "green locally, red in CI" divergence this gate exists to prevent.
`ruff.toml` then declares the rule set explicitly so a version bump can't move
the goalposts silently.

### Testing the CD path

A deploy can't be genuinely rehearsed locally — it needs AWS and a real CDK app.
What `--cdk-project` does instead is run the checks `cdk-review` would run,
against a real synthesized template tree:

```bash
./scripts/local-ci.sh --cdk-project ../bitwarden-cdk
```

That runs `cdk synth`, then validates bucket names against both the Python
source and the synthesized templates, then runs the IAM Access Analyzer check —
which needs real credentials and is skipped with a notice if `aws sts
get-caller-identity` fails. It also runs the workflow invariants against the
caller repo, which is where trigger-convention violations tend to live.

### Workflow invariants

`scripts/check_workflow_invariants.py` enforces the conventions in this document
that no off-the-shelf linter knows about — undeclared `workflow_call` secrets,
unpinned internal actions, `pull_request_target`, script injection into `run:`,
checks leaking into deploy workflows, README examples passing inputs that don't
exist. `--list-checks` prints all of them.

Findings that predate the checker are accepted in
`.github/workflow-invariants-baseline.yml`. That file is a **work list, not a
suppression list**: each entry cites the review finding it corresponds to, and
deleting an entry is how you claim the fix. A test asserts the baseline contains
no stale entries, so fixing something forces its entry out and a later
regression has nothing left to hide behind.

**New violations fail the gate at every severity, baselined ones don't.** The
gate always passes `--fail-on-warn`, so a newly-introduced `warn` finding blocks
just as an `error` does — the severities rank importance, they don't decide what
blocks. This matters because WF004 (mutable internal `@main`) and WF007
(forgeable `GITHUB_OUTPUT` delimiter) are `warn`, and those are two of the
security findings the review pins down; leaving them advisory in the mode CI
runs would have let a fresh violation of either through the required check.
Running the checker directly without `--fail-on-warn` is the lenient mode.

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

---

## Default PR Template

[`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) in this repo is picked up by GitHub as the **org-wide default pull request template**. Any repo in the `Specter099` org that doesn't define its own `.github/pull_request_template.md` will use it automatically — no per-repo setup required.
