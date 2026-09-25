# CI minutes — skip Dependabot, path filters, residual savings

**Date:** 2026-08-13
**Scope:** Shared reusable workflows in this repo, plus caller-side `on.pull_request` filters.
**Follows:** July consistency/efficiency wave ([#141](https://github.com/Specter099/.github/pull/141)), residual-minutes review ([#150](https://github.com/Specter099/.github/pull/150) / [#151](https://github.com/Specter099/.github/pull/151)).

Recommendations below are implemented in the accompanying PR unless marked as remaining / caller-side.

---

## Why Dependabot CI is waste

Dependabot in this org is configured for `github-actions` (weekly, grouped). Those PRs almost always rewrite SHA pins in workflow YAML. They do not change application code, CDK constructs, or CloudFormation.

Running `cdk-review` / `python-ci` / Access Analyzer on that class of PR still:

- boots a runner
- installs Python, Node, CDK, and `requirements.txt`
- synthesizes stacks and calls AWS

That is the same cost as a real feature PR, for a diff that yamllint and the invariants checker would accept unchanged. Skip the job instead. GitHub treats a skipped required check as passing, so branch protection does not block merge.

The skip lives **inside** the reusable workflows so every caller gets it without a migration:

```yaml
if: ${{ github.actor != 'dependabot[bot]' }}
```

Deploy and backup jobs are not skipped. If a Dependabot PR is merged, `push: [main]` still deploys.

This repo's own `self-test.yml` skips Dependabot for the same reason: a SHA swap does not change pytest, ruff, or invariant results.

---

## Why docs-only PRs still burn minutes

`paths-ignore` cannot be set on a `workflow_call` target. The July spec's caller wave (`paths-ignore: ['**/*.md', 'docs/**', 'LICENSE']`) never rolled out, so a README-only PR still runs full synth.

**This repo:** `self-test.yml` now has that `paths-ignore`.

**Callers:** must add it on their own `on.pull_request` block. Snippet is in `README.md` / `CLAUDE.md`. `WF016` warns when a PR-triggered check workflow has neither `paths-ignore` nor `paths`.

---

## Other minute savings in this change

These are the cheap leftovers from the August residual review, implemented here so they are not stranded on a separate draft:

| Change | Where | Effect |
|---|---|---|
| Drop duplicate `pip install -r requirements.txt` | `cdk-review` | setup-cdk already installed it |
| Reorder bandit/lint/tests before AWS | `cdk-review` | cheap failure skips synth |
| Hash `package-lock.json` in npm cache key | `setup-cdk` | cache was frozen forever |
| `cache-dependency-path` on pip | `setup-cdk`, `python-ci` | monorepo requirements hit the cache |
| Gate QEMU/Buildx | `static-site-deploy` | `enable-docker-bundling` default `false` |
| `ci-log-destination` default `s3` | five review/deploy/python-ci workflows | CloudWatch ingest was opt-out |
| Concurrency + cancel-in-progress | `gitleaks`, `validate-bucket-names`, `access-analyzer-check` | superseding PR pushes don't run to completion |
| Sparse-checkout of `scripts/` | `validate-bucket-names` | second clone was the whole repo |
| Shallow checkout | `repo-backup` | `git archive HEAD` does not need history |
| Fold `pip install --upgrade pip` | `python-ci` | one less step |
| Dependabot `cooldown: 7` days | `dependabot.yml` | fewer auto-PRs for the same bump |

---

## Remaining (not in this PR)

- **Caller `paths-ignore` rollout** — largest org-wide residual; this repo can only document + lint it (`WF016`).
- **Parallel job split** of `cdk-review` / `static-site-review` — wall-clock win; billed minutes can go *up* unless fail-fast gates the expensive jobs. Reorder-for-fail-fast (done) is the minute-oriented first step.
- **Frontend artifact handoff** (review uploads `dist/`, deploy downloads) — saves a rebuild and closes a drift window.
- **Internal action SHA pins** (`@main` → tag SHA) — security (S6 / WF004), not minutes.

Do **not** skip the pre-deploy `cdk synth`, and do **not** reuse the CDK Nag assembly for `cdk diff` (Nag Metadata would phantom-diff every PR).

---

## Invariants added

| ID | Severity | Rule |
|---|---|---|
| WF015 | warn | Check-workflow jobs with steps must skip Dependabot |
| WF016 | warn | PR-triggered check workflows must set `paths-ignore` or `paths` |

Jobs that only `uses:` a reusable workflow are exempt from WF015 (the callee owns the skip). `workflow_call`-only files are exempt from WF016 (they cannot filter paths).
