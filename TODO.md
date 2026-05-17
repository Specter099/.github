# Workflow Review — Remaining Work

Tracks follow-up work from the April 2026 GitHub Actions review. PRs [#45](https://github.com/Specter099/.github/pull/45) (script-injection fix) and [#46](https://github.com/Specter099/.github/pull/46) (Access Analyzer dedupe) already in flight. Everything below is scoped as a separate PR unless noted.

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

### Pin all actions to full SHA + add Dependabot
**Files:** every workflow + `actions/*/action.yml`
- Replace `@v4`, `@v7`, `@v2`, and internal `@main` refs with 40-char commit SHAs and a trailing `# vX.Y.Z` comment.
- Especially `Specter099/.github/.github/actions/{setup-cdk,access-analyzer,ship-logs}@main` — `@main` means any commit immediately propagates to every caller.
- Add `.github/dependabot.yml` with `package-ecosystem: github-actions` to auto-PR SHA bumps weekly.
- `python-ci.yml` already pins `actions/checkout`, `actions/setup-python`, and `gitleaks-action` by SHA — use as reference.

### Fix CDK Nag silent failure
**File:** [`cdk-review.yml:173-183`](.github/workflows/cdk-review.yml)
- `CDK_NAG=true cdk synth 2>&1 | grep … || true` swallows both `cdk synth` errors and grep's exit-1 on "no Nag findings".
- Fix: run `cdk synth` once (fail on error), capture output, then `grep … || true` on the captured file.

### Validate `CDK_STACKS` input in `cdk-deploy.yml`
**File:** [`cdk-deploy.yml:82-93`](.github/workflows/cdk-deploy.yml)
- `$CDK_STACKS` is passed unquoted to `cdk deploy` to allow `--all` or multiple stack names. Caller-controlled.
- Add a regex guard before exec: `[[ "$CDK_STACKS" =~ ^(--all|[A-Za-z0-9_\-]+( [A-Za-z0-9_\-]+)*)$ ]] || { echo "::error::invalid stacks input"; exit 1; }`

---

## P1 — Reliability & Hygiene

### Add `timeout-minutes` to every job
**Files:** all workflows
- Default is 6 hours — a hung step burns runner budget.
- Reviews: `timeout-minutes: 15`. Deploys: `timeout-minutes: 30`. Backup: `timeout-minutes: 10`.

### Consolidate `backup.yml` → `repo-backup.yml`
**Files:** [`backup.yml`](.github/workflows/backup.yml), [`repo-backup.yml`](.github/workflows/repo-backup.yml)
- Two near-identical backup jobs. `backup.yml` hardcodes `aws-region: us-east-1` and uses `environment: production` (backup doesn't need prod approvals).
- Make `backup.yml` call `repo-backup.yml` via `uses: ./.github/workflows/repo-backup.yml` with `environment: backup`.

### Remove dead CloudWatch `sequence_token` plumbing
**File:** [`actions/ship-logs/action.yml:111-133`](.github/actions/ship-logs/action.yml)
- `put-log-events --sequence-token` has been optional/ignored by CloudWatch since Aug 2023.
- Delete the token state-tracking code — simplifies the Python block substantially.

### Remove redundant `pip install` in `cdk-review.yml`
**File:** [`cdk-review.yml:69-72`](.github/workflows/cdk-review.yml)
- `setup-cdk` composite already installs `requirements.txt` (default `requirements-path`).
- Delete the separate `Install dependencies` step. Saves ~15–30s per run.

### Pin internal-action ref in `validate-bucket-names.yml`
**File:** [`validate-bucket-names.yml:36-38`](.github/workflows/validate-bucket-names.yml)
- Second `actions/checkout` pulls `Specter099/.github` at implicit `main`. Script changes silently alter caller behavior.
- After SHA-pinning work above, set `ref: <tag-or-sha>` here too.

### Drop `smoke-test-url` input from `cdk-review.yml`
**File:** [`cdk-review.yml:15-18`](.github/workflows/cdk-review.yml)
- Declared but never referenced. Vestigial from interface parity with deploy.
- Either remove or add a `# unused — kept for caller interface parity` comment.

---

## P2 — Code Smell / Consistency

### Reorder `cdk-review.yml` for faster failure
**File:** [`cdk-review.yml`](.github/workflows/cdk-review.yml)
- SAST (bandit) currently runs *after* `cdk synth` / Access Analyzer, both of which need AWS creds.
- Reorder: checkout → setup → lint → bandit → tests → pip-audit → *(AWS creds)* → synth → access-analyzer → nag → diff.

### Extract `ENABLE_LOGS` boilerplate to composite action
**Files:** every review/deploy workflow
- Each step repeats the same `if [ "$ENABLE_LOGS" = "true" ]; then … | tee … else … fi` block. ~30 lines of boilerplate per file.
- Create `actions/run-with-optional-log/action.yml` that takes `command:` and `log-file:` and handles the tee wrapping.

### Fix `find -maxdepth 1` in access-analyzer composite
**File:** [`actions/access-analyzer/action.yml:36`](.github/actions/access-analyzer/action.yml)
- `find "$YAML_DIR" -maxdepth 1 …` misses nested CloudFormation YAML directories.
- Remove `-maxdepth 1` or parameterize.

### Fix npm cache key in `setup-cdk`
**File:** [`actions/setup-cdk/action.yml:26-30`](.github/actions/setup-cdk/action.yml)
- `actions/cache` key includes `runner.os` and `cdk-version` but no `hashFiles('**/package-lock.json')` — cache never invalidates when the caller's JS deps change.
- Either drop the npm cache step entirely (only CDK CLI is installed globally, which is version-keyed) or add a proper hashed key + `restore-keys`.

### Standardize AWS role ARN source
**File:** [`cdk-deploy.yml:78`](.github/workflows/cdk-deploy.yml)
- `role-to-assume: ${{ secrets.AWS_ROLE_ARN || vars.AWS_ROLE_ARN }}` — mixed convention. Other workflows require `secrets.AWS_ROLE_ARN` only.
- Pick one: prefer `vars.AWS_ROLE_ARN` uniformly (ARN isn't secret) and document in `CLAUDE.md`.

### Reconcile `CLAUDE.md` with actual workflow
**Files:** [`CLAUDE.md`](CLAUDE.md), [`cdk-review.yml`](.github/workflows/cdk-review.yml)
- `CLAUDE.md` claims `cdk-review` runs **checkov**. No checkov step exists.
- Either add `checkov -d .` or remove the claim.

### Normalize YAML file headers
**Files:** all workflows
- `cdk-review.yml` and `python-ci.yml` start with `---` + `"on":`. Others don't.
- `.yamllint.yml` already tolerates both. Pick one and apply across the board for consistency.

### Close `gitleaks-action` license gap (contingent)
**File:** [`gitleaks.yml`](.github/workflows/gitleaks.yml), [`python-ci.yml`](.github/workflows/python-ci.yml)
- `gitleaks/gitleaks-action@v2` requires a paid license for private-org scans above a free-tier threshold.
- If that threshold is ever hit, swap to `docker://zricethezav/gitleaks:latest detect --source=. --redact`.

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
