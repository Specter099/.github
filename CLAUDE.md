# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Shared GitHub Actions reusable workflows and composite actions for Specter099 repositories. Provides standardized CI/CD pipelines for CDK projects, static site deployments, Python CI, repo backups, and security scanning.

## Directory Structure

```
.github/
  actions/
    access-analyzer/action.yml   # Composite action — IAM Access Analyzer no-public-access check
    setup-cdk/action.yml         # Composite action — install Python, Node, CDK CLI, pip deps
  workflows/
    access-analyzer-check.yml    # Reusable workflow — Access Analyzer scan of CFN templates
    backup.yml                   # (legacy) Repo backup workflow
    cdk-deploy.yml               # Reusable workflow — CDK deploy to production
    cdk-review.yml               # Reusable workflow — CDK lint, test, synth, diff on PRs
    gitleaks.yml                 # Reusable workflow — secret scanning with gitleaks
    python-ci.yml                # Reusable workflow — lint, format, pytest for Python projects
    repo-backup.yml              # Reusable workflow — git archive to S3
    static-site-deploy.yml       # Reusable workflow — frontend build + CDK deploy
    static-site-review.yml       # Reusable workflow — frontend + CDK PR checks
    validate-bucket-names.yml    # Reusable workflow — S3 naming convention enforcement
scripts/
  check_no_public_access.py      # Access Analyzer helper script
  validate_bucket_names.py       # S3 bucket name convention validator
```

## Common Commands

```
# Lint YAML files
yamllint -c .yamllint.yml .

# Run bucket name validator locally
python scripts/validate_bucket_names.py --path .
```

## Workflow Reference

| Workflow | Trigger | Purpose |
|---|---|---|
| `cdk-review` | `workflow_call` | PR check: lint, test, `cdk synth`, `cdk diff` + PR comment |
| `cdk-deploy` | `workflow_call` | Deploy CDK to `production` env, optional smoke test |
| `static-site-review` | `workflow_call` | PR check for frontend + CDK monorepos |
| `static-site-deploy` | `workflow_call` | Build frontend + deploy CDK |
| `python-ci` | `workflow_call` | Lint, format-check, gitleaks, pytest for pure Python |
| `repo-backup` | `workflow_call`, `workflow_dispatch` | Archive repo to S3 |
| `gitleaks` | `workflow_call` | Secret scanning (full git history) |
| `access-analyzer-check` | `workflow_call` | IAM Access Analyzer no-public-access check |
| `validate-bucket-names` | `workflow_call` | S3 bucket naming convention enforcement |

## Composite Actions

- **`setup-cdk`** — Installs Python 3.12, Node 22, pinned CDK CLI, and pip dependencies. Used internally by CDK workflows.
- **`access-analyzer`** — Runs `CheckNoPublicAccess` API against synthesized CloudFormation templates. Supports both JSON and YAML (via cfn-flip).

## Conventions

- All workflows assume a `production` GitHub environment with an `AWS_ROLE_ARN` secret for OIDC authentication.
- S3 bucket names must follow: `{purpose}-{account-id}-{region}-an`.
- Default CDK CLI version: `2.1106.1`. Default Python: `3.12`.
- Callers reference workflows as `Specter099/.github/.github/workflows/{name}.yml@main`.
