# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Shared GitHub Actions reusable workflows and composite actions for the Specter099 user account (not an org — no org-level secrets/variables exist). Provides standardized CI/CD pipelines for CDK projects, static sites (frontend + CDK infra), pure Python libraries, repo backups to S3, secret scanning, IAM Access Analyzer checks, and S3 bucket naming convention enforcement. Consumed by caller repos via `uses: Specter099/.github/.github/workflows/<name>@main`.

## Common Commands

# Lint all YAML files
yamllint -c .yamllint.yml .github/

# Validate bucket naming script locally
python scripts/validate_bucket_names.py --path /path/to/cdk/project

# Run access analyzer check locally (requires AWS credentials)
python scripts/check_no_public_access.py --template-dir /path/to/cdk.out

# Run this repo's own tests (same as self-test.yml CI)
pytest tests/ -v

## Directory Structure

.github/
  workflows/
    cdk-review.yml            # PR check: lint, test, synth, diff, SAST, CDK Nag, Access Analyzer
    cdk-deploy.yml            # Deploy CDK stacks to production
    static-site-review.yml    # PR check: frontend + CDK infra
    static-site-deploy.yml    # Build frontend + deploy CDK
    python-ci.yml             # Lint (ruff), format, gitleaks, pytest
    repo-backup.yml           # Archive repo to S3 (reusable)
    backup.yml                # Weekly self-backup (schedule: Sunday 02:00 UTC)
    access-analyzer-check.yml # IAM Access Analyzer public access check
    validate-bucket-names.yml # S3 bucket naming convention enforcement
    gitleaks.yml              # Secret scan (reusable wrapper)
    self-test.yml             # PR check for this repo itself (yamllint + pytest)
  actions/
    setup-cdk/action.yml      # Composite: Python 3.12 + Node 22 + CDK CLI
    access-analyzer/action.yml # Composite: scan CFN templates for public access
    ship-logs/action.yml      # Composite: upload step logs to S3/CloudWatch
scripts/
  check_no_public_access.py   # CLI for IAM Access Analyzer CheckNoPublicAccess API
  validate_bucket_names.py    # AST-based S3 bucket_name= convention checker
tests/                        # pytest suite for the helper scripts

## Architecture

All workflows use `workflow_call` triggers — caller repos reference them with `uses:` and pass inputs. AWS authentication is OIDC-based: callers must have an `AWS_ROLE_ARN` secret on their GitHub environment (default: `production`).

**Workflow dependency chain:**
- `cdk-review` and `cdk-deploy` both use the `setup-cdk` composite action
- `static-site-review` and `static-site-deploy` extend CDK workflows with frontend (npm) build/test steps
- `cdk-review` includes SAST (bandit), CDK Nag, and IAM Access Analyzer in addition to synth/diff (no checkov — it was removed deliberately)
- `python-ci` is standalone (no AWS credentials needed) — runs ruff, gitleaks, and pytest

**Trigger convention in caller repos:**
- All checks (review, security, tests, bucket-name validation) MUST trigger on `pull_request: [main]` only.
- `push: [main]` is reserved for deploy workflows (and scheduled backups).
- A caller workflow must never trigger on both `pull_request` and `push: [main]` — that double-runs the same checks at merge. Merge protection covers main-branch correctness; PR checks are the gate.

**PR diff commenting:** `cdk-review` and `static-site-review` post CDK diff output as a PR comment, updating in place on re-runs.

**Bucket naming convention:** `{prefix}-{12-digit-account-id}-{aws-region}-an`. The `validate-bucket-names` workflow checks both Python source (AST parsing for `bucket_name=` kwargs) and synthesized CloudFormation templates.

## Configuration

| Secret/Variable | Scope | Purpose |
|---|---|---|
| `AWS_ROLE_ARN` | Environment secret (`production`, `backup`) | IAM role ARN for OIDC federation (all AWS workflows) |
| `BACKUP_S3_BUCKET` | Repository variable | S3 bucket for repo backups (`backup.yml`) |
| `CI_LOGS_BUCKET` | Repository variable (caller repos) | Fallback S3 bucket for CI log shipping (`ship-logs`) |
| `CI_LOGS_LOG_GROUP` | Repository variable (caller repos) | Fallback CloudWatch log group for CI log shipping |
| `CDK_CLI_VERSION` | Repository variable (caller repos) | Fallback CDK CLI version for `setup-cdk` |

## Code Style

- YAML: `.yamllint.yml` config — line-length disabled, document-start disabled, truthy allows `on`
- Python scripts: no formatter config in repo — follow existing style (type hints, argparse CLI, boto3)
