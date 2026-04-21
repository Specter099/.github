# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Shared GitHub Actions reusable workflows and composite actions for the Specter099 org. Provides standardized CI/CD pipelines for CDK projects, static sites (frontend + CDK infra), pure Python libraries, repo backups to S3, secret scanning, IAM Access Analyzer checks, and S3 bucket naming convention enforcement. Consumed by caller repos via `uses: Specter099/.github/.github/workflows/<name>@main`.

## Common Commands

# Lint all YAML files
yamllint -c .yamllint.yml .github/

# Validate bucket naming script locally
python scripts/validate_bucket_names.py --path /path/to/cdk/project

# Run access analyzer check locally (requires AWS credentials)
python scripts/check_no_public_access.py --template-dir /path/to/cdk.out

## Directory Structure

.github/
  workflows/
    cdk-review.yml            # PR check: lint, test, synth, diff, SAST, IaC scan
    cdk-deploy.yml            # Deploy CDK stacks to production
    static-site-review.yml    # PR check: frontend + CDK infra
    static-site-deploy.yml    # Build frontend + deploy CDK
    python-ci.yml             # Lint (ruff), format, gitleaks, pytest
    repo-backup.yml           # Archive repo to S3 (reusable)
    backup.yml                # Weekly self-backup (schedule: Sunday 02:00 UTC)
    access-analyzer-check.yml # IAM Access Analyzer public access check
    validate-bucket-names.yml # S3 bucket naming convention enforcement
    gitleaks.yml              # Secret scan (reusable wrapper)
  actions/
    setup-cdk/action.yml      # Composite: Python 3.12 + Node 22 + CDK CLI
    access-analyzer/action.yml # Composite: scan CFN templates for public access
scripts/
  check_no_public_access.py   # CLI for IAM Access Analyzer CheckNoPublicAccess API
  validate_bucket_names.py    # AST-based S3 bucket_name= convention checker

## Architecture

All workflows use `workflow_call` triggers — caller repos reference them with `uses:` and pass inputs. AWS authentication is OIDC-based: callers must have an `AWS_ROLE_ARN` secret on their GitHub environment (default: `production`).

**Workflow dependency chain:**
- `cdk-review` and `cdk-deploy` both use the `setup-cdk` composite action
- `static-site-review` and `static-site-deploy` extend CDK workflows with frontend (npm) build/test steps
- `cdk-review` includes SAST (bandit), IaC scanning (checkov), and CDK Nag in addition to synth/diff
- `python-ci` is standalone (no AWS credentials needed) — runs ruff, gitleaks, and pytest

**PR diff commenting:** `cdk-review` and `static-site-review` post CDK diff output as a PR comment, updating in place on re-runs.

**Bucket naming convention:** `{prefix}-{12-digit-account-id}-{aws-region}-an`. The `validate-bucket-names` workflow checks both Python source (AST parsing for `bucket_name=` kwargs) and synthesized CloudFormation templates.

## Configuration

| Secret/Variable | Scope | Purpose |
|---|---|---|
| `AWS_ROLE_ARN` | Environment secret | IAM role ARN for OIDC federation (all AWS workflows) |
| `BACKUP_S3_BUCKET` | Environment variable | S3 bucket for repo backups (`backup.yml`) |

## Code Style

- YAML: `.yamllint.yml` config — line-length disabled, document-start disabled, truthy allows `on`
- Python scripts: no formatter config in repo — follow existing style (type hints, argparse CLI, boto3)
