# Workflow Review — Remaining Work

Open follow-up work. Completed items are removed rather than ticked off. The current prioritized findings are in [`docs/reviews/2026-09-25-security-ops-simplicity-review.md`](docs/reviews/2026-09-25-security-ops-simplicity-review.md). Everything below is scoped as a separate PR unless noted.

Mental model: **all checks in CI (review workflows), CD only deploys.** Do not add check steps to `cdk-deploy.yml` or `static-site-deploy.yml`.

Trigger convention in caller repos:
- All checks (tests, security scans, lint, synth/diff, access analyzer, bucket-name validation) MUST run on `pull_request: [main]` only.
- `push: [main]` is reserved for deploy workflows.
- Never run a check workflow on both `pull_request` and `push: main` — that double-runs the same checks on merge.

---

## P0 — Security & Correctness

### Fix caller-repo `security.yml` triggers
**Files (external repos):**
- `Specter099/static-site-infra/.github/workflows/security.yml`
- `Specter099/bitwarden-cdk/.github/workflows/security.yml`
- `Specter099/route53-cdk/.github/workflows/security.yml`

All three trigger on both `push: [main]` **and** `pull_request: [main]`. Per the trigger convention, security scans belong to PR flow only. Remove the `push:` block from each.

### Separate review role and environment (needs AWS + GitHub settings)
**Files:** `cdk-review.yml`, `static-site-review.yml`; IAM; each caller's environments
- Review jobs run PR-authored code and currently bind the `production` environment, so they can assume the deploy role (review S1).
- Workflow side already done: 900s session cap and `role-session-name`. Still needed: a read-only review role trusted on `repo:Specter099/<repo>:pull_request`, a `review` environment holding it, then flip the review workflows' `environment` default to `review`; restrict `production` to `main` and trust the deploy role only on `...:environment:production`.
- Order matters: flipping the default before callers have a `review` environment would break every review run.

### Pin internal actions/workflows (`@main`)
**Files:** every workflow that uses `Specter099/.github/.github/actions/*@main`
- Third-party actions are SHA-pinned and Dependabot bumps them; the internal `@main` refs are what's left (WF004 baseline).
- Tag this repo, pin internal `uses:` to the tag SHA, and let Dependabot bump it.

---

## P1 — Reliability & Hygiene

### Pin internal-action ref in `validate-bucket-names.yml`
**File:** [`validate-bucket-names.yml:36-38`](.github/workflows/validate-bucket-names.yml)
- Second `actions/checkout` pulls `Specter099/.github` at implicit `main`. Script changes silently alter caller behavior.
- After SHA-pinning work above, set `ref: <tag-or-sha>` here too.

---

## P2 — Code Smell / Consistency

### Extract `ENABLE_LOGS` boilerplate to composite action
**Files:** every review/deploy workflow
- Each step repeats the same `if [ "$ENABLE_LOGS" = "true" ]; then … | tee … else … fi` block. ~30 lines of boilerplate per file.
- Create `actions/run-with-optional-log/action.yml` that takes `command:` and `log-file:` and handles the tee wrapping.

### Fix `find -maxdepth 1` in access-analyzer composite
**File:** [`actions/access-analyzer/action.yml:36`](.github/actions/access-analyzer/action.yml)
- `find "$YAML_DIR" -maxdepth 1 …` misses nested CloudFormation YAML directories.
- Remove `-maxdepth 1` or parameterize.

### Move `backup.yml` off the `production` environment
**File:** [`backup.yml`](.github/workflows/backup.yml)
- Already calls `repo-backup.yml`, but still passes `environment: production`, so the backup runs under the prod environment and role.
- Switch it to `environment: backup` (the callee's default) once that environment has its own role.

---

## P3 — CI Coverage Parity

### Add missing checks to `static-site-review.yml`
**File:** [`static-site-review.yml`](.github/workflows/static-site-review.yml)
- Per the "all checks in CI" mental model, `static-site-review` should match `cdk-review`'s check depth for the infra portion.
- Missing vs `cdk-review`: SAST (bandit), CDK Nag, IAM Access Analyzer, pip-audit gating, bucket-name validation.
- Add these as optional/feature-flagged inputs (`enable-access-analyzer`, `enable-bandit`, etc.) so lightweight static sites aren't forced through the full gauntlet.

### Decide: CI-built frontend artifact vs. CD rebuilds?
**Files:** [`static-site-review.yml`](.github/workflows/static-site-review.yml), [`static-site-deploy.yml`](.github/workflows/static-site-deploy.yml)
- CI currently `npm run build`s for validation; CD `npm run build`s again before deploy — redundant work and a potential drift source (different node versions, cache state, etc.).
- Option A: CI uploads `dist/` artifact, CD downloads it. Reproducible, faster CD, but adds artifact plumbing.
- Option B: Keep current. CI build is validation-only; CD rebuild is the canonical deploy artifact.
- Decide and document.

---

## Hygiene

### Add SECURITY.md and CODEOWNERS
- This is the account's special `.github` repo — also the natural home for a `profile/README.md` if one is ever wanted.

### Move ship-logs Python heredoc to `scripts/`
**File:** [`actions/ship-logs/action.yml`](.github/actions/ship-logs/action.yml)
- The embedded Python block would be unit-testable as `scripts/ship_logs.py`.
