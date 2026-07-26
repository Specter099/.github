# CI/CD Consistency & Efficiency — Design Spec

**Date:** 2026-07-26
**Scope:** `Specter099/.github` reusable workflows/actions, plus caller workflows in ~20 consuming repos.

## Goals

1. Remove deployment inconsistencies between the CDK and static-site workflow families (secrets handling, deploy safety, smoke-test behavior, trigger conventions).
2. Reduce billed GitHub Actions minutes without weakening the actual CI/CD gate.
3. Establish one clear trigger convention — CI runs on PR open/update; CD runs on merge to `main`; CD never re-runs the CI suite — and bring every caller repo into line with it.

## Non-goals

- Rewriting `bitwarden-cdk/cdk-deploy.yml`'s VPC-origin-toggle logic or `sample-lambda-microvm-claude-managed-agents/cd-web.yml`'s two-plane deploy split. Both intentionally diverge from the shared template for documented, repo-specific reasons and are left untouched.
- Changing the bucket-naming or Access-Analyzer *policy* logic (`scripts/*.py`) — only the workflows that invoke them.
- Standardizing manual (`workflow_dispatch`-only) vs. automatic (`push`-triggered) deploy across repos — that's a per-repo ops choice, not an inconsistency to fix. Noted as a finding only.

## Current-state findings (baseline)

**Minutes usage** (30-day sample across private caller repos, extrapolated from job durations):

| Workflow | Runs/30d | Avg min | Est. min/30d |
|---|---|---|---|
| CI (`python-ci`) | 140 | 6.2 | 875 |
| CD (`cdk-deploy`) | 106 | 5.5 | 583 |
| Backup to S3 | 105 | 1.5 | 158 |
| Security — Access Analyzer | 59 | 2.0 | 118 |
| Review (`cdk-review`/`static-site-review`) | 69 | 1.5 | 104 |
| Deploy (`static-site-deploy`) | 29 | 3.2 | 94 |
| **Total (sampled)** | 660 | | **~2,250 min/mo** |

**Deployment inconsistencies:**
- `cdk-deploy` accepts a `stacks` input; `static-site-deploy` always runs bare `cdk deploy` (no stack targeting).
- `static-site-deploy` sets up QEMU + Buildx; `cdk-deploy` doesn't (not needed, but the two families otherwise mirror each other everywhere else).
- Neither deploy workflow declares a `secrets:` block — both rely on implicit `secrets: inherit` from the caller, with no validation that `AWS_ROLE_ARN` is actually present.
- Neither deploy re-verifies anything before deploying — no synth, no diff, no drift check between review-time and deploy-time state.
- The smoke test can never fail the job: `curl ... || echo "000"` followed by `::warning::` only. A dead endpoint still reports green.
- `backup.yml` (this repo's own weekly backup) duplicates `repo-backup.yml` verbatim (~70 lines) instead of calling it.
- Trigger conventions are inconsistent across caller repos: **9 of 14** hand-rolled `security.yml` files trigger on both `push` and `pull_request` to `main`, directly violating this repo's own documented convention (checks are PR-only; push is reserved for deploy/schedule).
- 14 caller repos hand-roll ~25 lines of raw Access-Analyzer setup in `security.yml` instead of calling the reusable `access-analyzer-check.yml`, so they get none of the shared workflow's caching, version pinning, or future fixes.

**Minutes-waste specifics:**
- `cdk-review` synthesizes **three times** per run (`cdk synth`, `CDK_NAG=true cdk synth`, then `cdk diff`, which re-synths internally).
- No `concurrency` group on any review/CI workflow — every push to a PR branch runs a full parallel job; nothing cancels the superseded run. `python-ci.yml` only leaves a code comment telling callers to configure this themselves — never enforced.
- No path filters anywhere — a docs-only PR still runs full CDK synth, bandit, pip-audit, npm build.
- `enable-ci-logs` defaults to `true` in the five workflows that expose the input (`cdk-review`, `cdk-deploy`, `static-site-review`, `static-site-deploy`, `python-ci`), adding tee/tar/metadata/S3/CloudWatch overhead to every run whether or not the caller needs shipped logs. (`gitleaks.yml`, `validate-bucket-names.yml`, `access-analyzer-check.yml`, and the backup workflows have no such input and are unaffected.)
- Job timeouts are inconsistent and loose relative to observed run times (see table below) — a genuinely hung job burns significantly more paid minutes than necessary before GitHub kills it.

**Timeout audit** (current vs. observed avg vs. new target, ~2-3x observed avg with headroom for cold caches):

| Workflow | Current | Observed avg | New target |
|---|---|---|---|
| `cdk-deploy.yml` | 45 min | 5.5 min | 15 min |
| `static-site-deploy.yml` | 45 min | ~5-6 min (docker cross-arch build adds time) | 20 min |
| `cdk-review.yml` | 20 min | ~1.5-2.5 min | 10 min |
| `static-site-review.yml` | 20 min | ~1.5-2.5 min | 10 min |
| `python-ci.yml` | 20 min | 6.2 min | 12 min |
| `self-test.yml` | 15 min | (small repo, fast) | 5 min |
| `gitleaks.yml` | 15 min | (typically <1 min) | 10 min |
| `access-analyzer-check.yml` | 15 min | 2.0 min | 8 min |
| `backup.yml` / `repo-backup.yml` | 15 min | 1.5 min | 8 min |
| `validate-bucket-names.yml` | 10 min | (checkout + script only) | 5 min |

## Architecture: trigger convention

- **CI** (`python-ci`, `cdk-review`, `static-site-review`, and the `security.yml` callers after migration) triggers on `pull_request: [main]` only.
- **CD** (`cdk-deploy`, `static-site-deploy`) triggers on `push: [main]` only, optionally plus `workflow_dispatch` for manual re-runs.
- **The CI gate for merges is GitHub branch protection**, not re-execution: `main` requires the CI workflows as required status checks, so by the time a push to `main` triggers CD, lint/tests/secret-scan have already passed. CD does **not** re-run pytest/ruff/bandit/gitleaks.
- **CD's own pre-deploy check is operational, not CI**: a fresh `cdk synth` immediately before `cdk deploy` (catches drift — e.g. a merge race with another PR merged after this branch's review ran), reusing that synth via `cdk deploy --app cdk.out` rather than letting `deploy` re-synth from scratch. A smoke test after deploy that **fails the job** on non-2xx/3xx or curl failure (with a `smoke-test-required` opt-out input, default `true`, for repos with known propagation delay).
  - `smoke-test-required` only changes behavior when `smoke-test-url` is actually set — it does not change today's `if: inputs.smoke-test-url != ''` gate, so a repo with no smoke-test URL configured still skips the step entirely regardless of this input. When a URL *is* set: `smoke-test-required: true` (default) fails the job on a bad/unreachable response; `false` preserves today's warn-only behavior.
  - `cdk-deploy.yml` doesn't run CDK Nag, so this synth-reuse is unaffected by the Nag/Metadata caveat below — it applies only to `cdk-review.yml`.

## Shared-repo changes (`Specter099/.github`)

| File | Change |
|---|---|
| `cdk-review.yml` | **Correction from earlier draft:** the plain `cdk synth` and the `CDK_NAG=true cdk synth` are *not* interchangeable — `cdk_nag.NagSuppressions.add_resource_suppressions_by_path` (used by every caller's `app.py`, confirmed in `bitwarden-cdk/app.py`) injects `Metadata.cdk_nag` blocks into the synthesized template, so a Nag-enabled synth's `cdk.out` differs from a plain one. Reusing it for `cdk diff` would surface phantom Metadata-only diffs on every PR. Instead: keep the plain `cdk synth` and the `CDK_NAG=true cdk synth` as two separate synths (unchanged from today), but reuse the **plain** synth's `cdk.out` for `cdk diff --app cdk.out` — removing only the diff's redundant implicit re-synth (3 synths → 2). Add job-level `concurrency: { group: cdk-review-${{ github.ref }}, cancel-in-progress: true }`. Flip `enable-ci-logs` default to `false`. Timeout → 10 min. |
| `cdk-deploy.yml` | Add explicit `secrets: { AWS_ROLE_ARN: { required: true } }`. Add a `cdk synth` step immediately before deploy; `cdk deploy --app cdk.out` reuses it (no Nag involved here, so the Metadata caveat above doesn't apply). Smoke test hard-fails on bad status (new `smoke-test-required` input, default `true`; see semantics note above). Flip `enable-ci-logs` default to `false`. (Already has a `stacks` input — no change needed there; see `static-site-deploy.yml` row for the parity fix.) Timeout → 15 min. |
| `static-site-review.yml` / `static-site-deploy.yml` | Mirror the same diff-synth-reuse, concurrency, secrets-block, smoke-test-hard-fail, and ci-logs-default treatment as their CDK counterparts. **Additionally**, add a `stacks` input to `static-site-deploy.yml` (default `--all`), mirroring `cdk-deploy.yml`'s existing input — this closes the stack-targeting inconsistency flagged under Goal 1 rather than leaving it unresolved. The new pre-deploy `cdk synth` step in `static-site-deploy.yml` must run `working-directory: ${{ inputs.infra-dir }}` and be inserted *after* the existing QEMU/Buildx setup steps (needed for Docker-asset bundling during synth) and *immediately before* the existing "CDK Deploy" step, not at the top of the job. Timeouts → 10 min / 20 min respectively. |
| `python-ci.yml` | Add job-level `concurrency: { group: python-ci-${{ github.ref }}-${{ matrix.python-version }}, cancel-in-progress: true }` — the group key **must** include `matrix.python-version`, otherwise the workflow's own Python-version matrix jobs would cancel each other instead of running in parallel (caught in spec review). Flip `enable-ci-logs` default to `false`. Timeout → 12 min. |
| `backup.yml` | Replace duplicated body with `uses: ./.github/workflows/repo-backup.yml`, `with: { s3-bucket: vars.BACKUP_S3_BUCKET, environment: production }`. Removes ~70 duplicated lines. Timeout (inherited from `repo-backup.yml`) → 8 min. |
| `access-analyzer-check.yml` | No functional change — this is the target callers migrate onto. Timeout → 8 min. |
| `gitleaks.yml`, `validate-bucket-names.yml`, `repo-backup.yml`, `self-test.yml` | Timeout tightened per table above (`self-test.yml` → 5 min). No other changes. |
| New: `actions/log-metadata/action.yml` | Extract the duplicated 30-line "generate log metadata" Python block (currently inlined in all 5 workflows that expose `enable-ci-logs`: `cdk-review.yml`, `cdk-deploy.yml`, `python-ci.yml`, `static-site-review.yml`, `static-site-deploy.yml`) into a composite action; each of the 5 calls it instead of inlining. |

## Caller-repo migration (second wave, ~20 repos)

1. **`security.yml` → `access-analyzer-check.yml`**: for the 14 repos with hand-rolled Access-Analyzer steps, replace the whole job body with `uses: Specter099/.github/.github/workflows/access-analyzer-check.yml@main`, matching each repo's existing `template-dir`/`environment`. While touching each file, fix the 9 repos triggering on both `push` and `pull_request` → `pull_request: [main]` only.
2. **Path filters**: `paths-ignore` can only live in the caller's own `on:` block (a `workflow_call` target can't filter paths). Add `paths-ignore: ['**.md', 'docs/**', 'LICENSE']` to each caller's PR-triggered workflow (review, security, CI callers).
3. **`enable-ci-logs` opt-in**: since the shared default flips to `false`, any repo that currently benefits from shipped logs (has `CI_LOGS_BUCKET`/`CI_LOGS_LOG_GROUP` vars set) needs `enable-ci-logs: true` added explicitly to keep that behavior. Everyone else gets the cheaper default silently.
4. Leave `bitwarden-cdk/cdk-deploy.yml` and `sample-lambda-microvm-claude-managed-agents/cd-web.yml` untouched (see Non-goals).

## Rollout order

1. PR to `Specter099/.github` with all shared-workflow changes; `self-test.yml` (yamllint + pytest) must pass; merge to `main`.
2. **Pre-work**: re-enumerate the current caller-repo list directly against the GitHub API at implementation time — not from this document. During spec-writing, 14 repos (including the originally-planned pilot, `frr-cdk`, and 4 other caller repos: `resource-endpoints`, `fedramp-high-security-hub-cdk`, `nist-800-53-security-hub-cdk`, `account-vending-machine`) were deleted mid-session as part of unrelated, confirmed-intentional account cleanup. Treat the caller-repo list in this spec as a snapshot, not a source of truth — confirm each repo still exists before opening a PR against it. From the surviving set, inventory which repos have `CI_LOGS_BUCKET`/`CI_LOGS_LOG_GROUP` vars set (need explicit `enable-ci-logs: true` to keep current behavior) and which `security.yml` files trigger on both `push` and `pull_request` (need the trigger fix). Also don't trust local clones under `~/Documents/GitHub/` as a stand-in for this check — one was confirmed months out of date relative to its remote during spec review.
3. Pilot caller PR — **`wordpress-cdk`** (confirmed via the GitHub API to currently have all four workflow types: backup, cdk-review, cdk-deploy, security, plus a `security.yml` that already exhibits the push+PR trigger issue, making it a good end-to-end test of that fix too) — validate end-to-end before fanning out.
4. Remaining caller PRs (count depends on the pre-work re-enumeration in step 2 — treat "~20" elsewhere in this document as approximate), opened in batches by change type: (a) `security.yml` migrations + trigger fixes, (b) path-filter/ci-logs cleanup on the rest.

## Testing & validation

- This repo: `self-test.yml` (yamllint + pytest) covers script and YAML correctness; extend it to lint the new `actions/log-metadata` composite.
- No dry-run environment exists for the reusable workflows themselves — the pilot repo (`frr-cdk`) is the real integration test. Its PR must show a green `cdk-review` run (confirming single-synth reuse and concurrency work) before fanning out further.
- Each subsequent caller PR is validated by its own green run before merge; no bulk/blind merge.

## Open risks

- Flipping `enable-ci-logs` default to `false` is a silent behavior change for any caller currently relying on inherited-default logging without realizing it. Mitigated by explicitly re-enabling it wherever `CI_LOGS_BUCKET`/`CI_LOGS_LOG_GROUP` vars are already configured (signal of real usage).
- `cdk deploy --app cdk.out` assumes the synth step immediately precedes it in the same job/workspace; any future refactor that separates these into different jobs would need to re-upload/download the `cdk.out` artifact instead.
- The smoke-test hard-fail is a behavior change that could newly fail deploys for repos with real propagation delay (e.g., CloudFront). The `smoke-test-required` opt-out (default `true`) exists specifically to de-risk this, but the first deploy after rollout on each repo should be watched.
