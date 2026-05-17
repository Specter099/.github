# Design: General Python CI Workflow

**Date:** 2026-03-03
**Status:** Approved

## Goal

A reusable `workflow_call` workflow for pure Python projects (no CDK, no AWS). Runs ruff linting, ruff format check, gitleaks secrets scan, and pytest.

## File

`.github/workflows/python-ci.yml`

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `python-version` | no | `"3.12"` | Python version for `actions/setup-python` |
| `requirements-path` | no | `"requirements-dev.txt"` | Path to dev requirements file |
| `tests-dir` | no | `"tests/"` | Directory passed to pytest |

## Permissions

`contents: read` only. No AWS credentials, no PR write access needed.

## Steps (in order)

1. `actions/checkout@v4` — `fetch-depth: 0` (full history required for gitleaks)
2. `actions/setup-python@v5` — installs the requested Python version
3. `pip install -r <requirements-path>` — installs dev dependencies
4. `ruff check .` — linting
5. `ruff format --check .` — format enforcement
6. `gitleaks-action` (`zricethezav/gitleaks-action@v2`) — secrets scan over full git history; uses `GITHUB_TOKEN` implicitly
7. `pytest <tests-dir> -v` — unit tests

## Design Decisions

- **Single job, sequential steps** — mirrors existing repo style; tools are fast enough that parallelism adds no meaningful benefit
- **gitleaks** — chosen for git-history awareness and official GitHub Action support; no baseline file management needed
- **ruff format --check** — enforces formatting as a hard gate, not just linting
- **`fetch-depth: 0`** — required for gitleaks to scan full commit history, not just the latest commit
- **No secrets required** — `GITHUB_TOKEN` is injected automatically by GitHub Actions

## Usage Example

```yaml
jobs:
  ci:
    uses: Specter099/.github/.github/workflows/python-ci.yml@main
    with:
      python-version: "3.12"         # optional
      requirements-path: requirements-dev.txt  # optional
      tests-dir: tests/              # optional
```
