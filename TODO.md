# Workflow Review — Remaining Work

Tracks follow-up work from the April 2026 GitHub Actions review (updated June 2026). Everything below is scoped as a separate PR unless noted.

Mental model: **all checks in CI (review workflows), CD only deploys.** Do not add check steps to `cdk-deploy.yml` or `static-site-deploy.yml`.

Trigger convention in caller repos:
- All checks (tests, security scans, lint, synth/diff, access analyzer, bucket-name validation) MUST run on `pull_request: [main]` only.
- `push: [main]` is reserved for deploy workflows.
- Never run a check workflow on both `pull_request` and `push: main` — that double-runs the same checks on merge.

---

## Done / In flight

- ✅ Fix caller-repo `security.yml` triggers (static-site-infra, bitwarden-cdk, route53-cdk) — all three now trigger on `pull_request` only.
- ✅ Pin all third-party actions to full SHA + add Dependabot — [#73](https://github.com/Specter099/.github/pull/73). Internal composite refs (`setup-cdk`, `access-analyzer`, `ship-logs`) stay on `@main` deliberately: this is a single-user account with merge protection, and pinning self-references would require a two-phase bump on every action change. Revisit if reproducible caller runs become a requirement (tagged releases).
- ✅ Add `timeout-minutes` to every job — [#73](https://github.com/Specter099/.github/pull/73).
- 🔄 Fix CDK Nag silent failure — [#84](https://github.com/Specter099/.github/pull/84).
- 🔄 Validate `CDK_STACKS` input in `cdk-deploy.yml` — [#84](https://github.com/Specter099/.github/pull/84).
- 🔄 Drop `eval` of `install-command` in `python-ci.yml` — [#84](https://github.com/Specter099/.github/pull/84).
- 🔄 Script robustness (f-string validation, stringified policies, exit code 2 for incomplete scans) — [#85](https://github.com/Specter099/.github/pull/85).
- 🔄 Consolidate `backup.yml` → `repo-backup.yml` — [#86](https://github.com/Specter099/.github/pull/86).
- 🔄 Remove dead CloudWatch `sequence_token` plumbing — [#86](https://github.com/Specter099/.github/pull/86).
- 🔄 Remove redundant `pip install` in `cdk-review.yml` — [#86](https://github.com/Specter099/.github/pull/86).
- 🔄 Pin internal scripts checkout in `validate-bucket-names.yml` (`job.workflow_sha`) — [#86](https://github.com/Specter099/.github/pull/86).
- 🔄 Document `smoke-test-url` as unused in `cdk-review.yml` (kept — a caller passes it) — [#86](https://github.com/Specter099/.github/pull/86).
- 🔄 Self-test now runs ruff, bandit, and actionlint — [#86](https://github.com/Specter099/.github/pull/86).
- 🔄 Reconcile `CLAUDE.md` with actual workflow (checkov claim removed) — this PR.

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

### Enable `ruff format --check` in self-test
**File:** [`self-test.yml`](.github/workflows/self-test.yml)
- Deferred from [#86](https://github.com/Specter099/.github/pull/86) because [#85](https://github.com/Specter099/.github/pull/85) reformats the test files. Once #85 lands, add `ruff format --check scripts/ tests/`.

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

---

## Hygiene (from June 2026 review)

### Add SECURITY.md and CODEOWNERS
- This is the account's special `.github` repo — also the natural home for a `profile/README.md` if one is ever wanted.

### Move ship-logs Python heredoc to `scripts/`
**File:** [`actions/ship-logs/action.yml`](.github/actions/ship-logs/action.yml)
- The embedded Python block would be unit-testable as `scripts/ship_logs.py`.
