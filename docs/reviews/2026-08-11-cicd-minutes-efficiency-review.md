# CI/CD Minutes Efficiency Review

**Date:** 2026-08-11
**Scope:** `.github/workflows/*.yml`, `.github/actions/*/action.yml` — residual minute waste after the July consistency/efficiency wave ([#139](https://github.com/Specter099/.github/pull/139) / [#141](https://github.com/Specter099/.github/pull/141)).
**Baseline usage:** ~2,250 min/mo across sampled caller repos (from the 2026-07-26 design spec).

This is a recommendations document. No workflow behaviour was changed.

---

## Verdict

The July wave already captured the largest easy wins: concurrency cancellation on review/CI jobs, `cdk diff --app cdk.out` (3 synths → 2), `enable-ci-logs` default `false`, and tighter timeouts. Remaining waste is smaller per-run but still material across the fleet — roughly **400–700 min/mo** recoverable in this repo's shared workflows, plus a larger (caller-side) saving from path filters that never landed.

Highest leverage next, in order:

1. Fix the frozen npm cache key in `setup-cdk` (every static-site run).
2. Drop the duplicate `pip install -r requirements.txt` in `cdk-review`.
3. Gate QEMU/Buildx behind an input in `static-site-deploy`.
4. Split review workflows into parallel jobs (wall-clock + credential isolation).
5. Ship caller `paths-ignore` (docs-only PRs still pay full CI).

---

## Already fixed (do not re-open)

| Item | Status |
|---|---|
| Concurrency + `cancel-in-progress` on review/CI | Done in `cdk-review`, `static-site-review`, `python-ci`, `self-test` |
| `cdk diff --app cdk.out` / `cdk deploy --app cdk.out` | Done |
| `enable-ci-logs` default `false` | Done |
| Timeout tightening | Done (matches July targets) |
| `backup.yml` → calls `repo-backup.yml` | Done |

---

## Remaining opportunities (ranked by estimated minutes saved)

Estimates use the July 30-day sample rates. Per-run savings are approximate; monthly figures assume the same run volume.

### 1. Frozen npm cache — `setup-cdk` *(High)*

**File:** `actions/setup-cdk/action.yml:74-78`

```yaml
key: npm-${{ runner.os }}-cdk-${{ steps.cdk-ver.outputs.version }}
```

No content hash → exact key hit on every run after the first → `actions/cache` skips post-run save → cache permanently frozen at whatever `~/.npm` looked like on day one for that CDK version. Caller `package-lock.json` changes never invalidate it, so `static-site-review` / `static-site-deploy` re-download frontend deps from the registry on every run after the first lockfile change.

**Fix:** hash the lockfile and prefer `setup-node`'s built-in cache:

```yaml
- uses: actions/setup-node@...
  with:
    node-version-file: ...
    node-version: ...
    cache: npm
    cache-dependency-path: '**/package-lock.json'
```

Drop the separate `actions/cache` step (or keep it only for the global CDK CLI install, keyed on CDK version alone).

**Est. saving:** 30–90s per static-site run when lockfile has drifted; ~29 deploy + ~half of 69 review runs touch npm → **~20–60 min/mo**, plus faster cold-cache recovery whenever CDK version bumps.

Also worth: pass `cache-dependency-path: ${{ inputs.requirements-path }}` to `setup-python` so monorepo callers with non-root requirements hit the pip cache reliably.

---

### 2. Duplicate `pip install -r requirements.txt` — `cdk-review` *(High, cheap)*

**File:** `cdk-review.yml:86-96`

`setup-cdk` already installs `requirements.txt` (default `requirements-path`). The next step installs it again, then optionally `requirements-dev.txt`.

**Fix:** keep only the `requirements-dev.txt` half:

```yaml
- name: Install dev dependencies
  run: |
    if [ -f requirements-dev.txt ]; then
      pip install -r requirements-dev.txt
    fi
```

**Est. saving:** 15–45s per `cdk-review` run (aws-cdk-lib + boto3 re-resolve). At ~69 review runs/mo → **~20–50 min/mo**. Four-line change; tracked since TODO.md P1 / July E3, still open.

---

### 3. Unconditional QEMU + Buildx — `static-site-deploy` *(High)*

**File:** `static-site-deploy.yml:137-143`

Both steps run on every deploy. They exist for arm64 Docker asset bundling during synth. A pure S3 + CloudFront static site needs neither.

**Fix:** gate behind `enable-docker-bundling` (default `false`). Callers that bundle Lambda/container assets set it true.

**Est. saving:** 30–60s per static-site deploy × ~29 runs/mo → **~15–30 min/mo**. Also cuts image-pull noise on the critical path.

---

### 4. Serial single-job review pipelines *(Medium–High)*

**Files:** `cdk-review.yml`, `static-site-review.yml`

Everything runs in one job. Lint / bandit / pytest / pip-audit do not need AWS credentials or each other, yet they sit on the critical path ahead of (and bandit sits *after*) credentialed synth/diff. CDK Nag is a second full synth that is independent of the plain synth + diff path.

**Proposed split** (also improves credential blast radius — July S1):

| Job | Needs AWS? | Work |
|---|---|---|
| `static-checks` | No | checkout → setup → lint → bandit → pytest → pip-audit |
| `infra` | Yes | checkout → setup → configure-aws → synth → access-analyzer → diff → comment |
| `nag` | Yes (or none, if Nag is local-only) | checkout → setup → `CDK_NAG=true cdk synth` |
| `ship-logs` | Yes | `needs: […]`, `if: always()`, gated on `enable-ci-logs` |

Wall clock becomes `max(...)` instead of `sum(...)`. Setup cost repeats (~20–30s/job, mostly cache-served); parallelism still wins once synth or npm build exceeds ~1 min.

**Est. saving:** wall-clock only on billed minutes when jobs overlap — GitHub bills per job-minute, so parallel jobs can *increase* billed minutes while cutting PR wait time. Treat this as a **latency** win first; minute savings come only if fail-fast cancels the other jobs (`fail-fast` via a thin orchestrator or by making `static-checks` a required predecessor of the expensive jobs so a lint failure skips synth entirely).

**Better minute-oriented variant:** keep one job, but **reorder** so bandit/lint/tests run before AWS + synth (TODO.md P2). A failing bandit then skips the expensive synth/diff/Nag path entirely — pure minute win, no parallel-job tax.

**Est. saving (reorder only):** depends on failure rate; at even a 10% early-fail rate on ~69 runs averaging ~2 min of AWS work → **~10–15 min/mo**, plus faster feedback. Cost: near zero.

---

### 5. Caller path filters never shipped *(High — caller side)*

Reusable `workflow_call` targets cannot filter paths. The July spec's second wave (`paths-ignore: ['**.md', 'docs/**', 'LICENSE']` on every caller PR workflow) was never rolled out.

A docs-only PR today still pays full `cdk-review` / `python-ci` / Access Analyzer.

**Est. saving:** highly repo-dependent. If ~15–25% of PRs are docs/config-only across the fleet, that's **~150–400 min/mo** — likely the single largest remaining org-wide saving, but it lives outside this repo.

**Action here:** document the required `paths-ignore` snippet in `README.md` / `CLAUDE.md` so callers can adopt without waiting on a migration PR wave; optionally add a WF invariant that warns when a caller workflow (checked via `--path`) has no `paths-ignore` on `pull_request`.

---

### 6. Frontend built twice — review then deploy *(Medium)*

**Files:** `static-site-review.yml:133-143`, `static-site-deploy.yml:119-129`

CI builds for validation; CD rebuilds before deploy. Pays twice and opens a drift window (different runner image / cache state).

**Fix (Option A from TODO.md P3):** upload `dist/` as an artifact from review; deploy downloads it. Deploy concurrency already serialises deploys, so artifact freshness is manageable if keyed on `github.sha`.

**Est. saving:** one full `npm run build` per merge (~30–90s) × ~29 deploys/mo → **~15–40 min/mo**, plus correctness.

---

### 7. `fetch-depth: 0` then `git archive HEAD` — backup *(Medium)*

**File:** `repo-backup.yml:67-70, 88-94`

Full history is fetched, then `git archive … HEAD` discards it. On large repos this dominates the job.

**Fix (pick one):**

- Snapshot intent (current behaviour): drop `fetch-depth: 0` → shallow checkout, archive tip only.
- Restorable backup intent: replace zip-of-HEAD with `git bundle --all` (justifies the full fetch).

**Est. saving:** tens of seconds to minutes per backup on large repos × ~105 backup runs/mo → **~30–100 min/mo** if several callers are large; negligible for small repos.

---

### 8. Full second checkout for two scripts — `validate-bucket-names` *(Medium)*

**File:** `validate-bucket-names.yml:37-42`

Clones all of `Specter099/.github` to run `scripts/validate_bucket_names.py`.

**Fix:**

```yaml
with:
  repository: Specter099/.github
  path: .shared-github
  sparse-checkout: scripts/
  sparse-checkout-cone-mode: false
  ref: <pinned tag or SHA>   # also closes July S6 for this path
```

**Est. saving:** small per run (seconds), but this is meant to be a fast gate — checkout is a large share of its runtime. Worth doing when SHA-pinning internal refs lands.

Also missing here: **concurrency** (`cancel-in-progress: true`). Same for `access-analyzer-check.yml` and `gitleaks.yml` when called from PR workflows that push rapidly.

---

### 9. `ci-log-destination` still defaults to `both` *(Low–Medium)*

**Files:** all five workflows that expose the input

`enable-ci-logs` correctly defaults to `false`, so this only bites opted-in callers. Those callers still pay S3 *and* per-line CloudWatch ingest (July E7) unless they override.

**Fix:** default `ci-log-destination` to `s3` (matches the composite's own default). CloudWatch becomes opt-in.

**Est. saving:** mostly AWS $ and ship-logs step time, not GitHub minutes — but the CloudWatch path is a multi-second Python loop per run when enabled.

---

### 10. Small / local cleanups *(Low)*

| Item | File | Saving |
|---|---|---|
| Dedicated `pip install --upgrade pip` step | `python-ci.yml:125-126` | ~5–10s/run; fold into install |
| Ad-hoc `pip install bandit` / `pip-audit` / `pytest-cov` at runtime | `cdk-review`, `static-site-review`, `python-ci`, `access-analyzer` | cache miss + resolve every run; pin in a shared `requirements-ci.txt` instead (also July S8) |
| `access-analyzer-check` has no pip cache | `access-analyzer-check.yml` | seconds; add `cache: pip` |
| No concurrency on `validate-bucket-names` / `access-analyzer-check` / `gitleaks` / `repo-backup` | those workflows | cancels superseded PR runs |
| gitleaks `fetch-depth: 0` always | `python-ci.yml`, `gitleaks.yml` | needed for full-history scans; on `pull_request` consider scanning only the PR commit range to allow shallow fetch |

---

## What *not* to do for minutes

- **Parallelising without fail-fast gating** can raise billed minutes (N jobs × setup) while only improving wall-clock. Prefer reorder-for-fail-fast first; parallelise only when jobs are independently required and each is long.
- **Cutting the pre-deploy `cdk synth`** in deploy workflows — it is intentional drift detection, not waste. Keep it; keep `--app cdk.out` reuse.
- **Dropping CDK Nag's second synth** by reusing plain `cdk.out` — Nag injects Metadata that would phantom-diff every PR. Already correctly isolated to `cdk-nag.out`.

---

## Suggested sequencing

**This repo, cheap first (one PR):**
1. Drop duplicate `pip install` in `cdk-review` (#2).
2. Fix npm / pip cache keys in `setup-cdk` (#1).
3. Gate QEMU/Buildx (#3).
4. Reorder `cdk-review` so bandit/lint/tests precede AWS (#4 variant).
5. Default `ci-log-destination: s3` (#9).
6. Add concurrency to `validate-bucket-names`, `access-analyzer-check`, `gitleaks` (#8/#10).
7. Sparse-checkout (and pin `ref`) for the shared-scripts checkout (#8).
8. Decide backup shallow-vs-bundle (#7).

**This repo, structural:**
9. Parallel job split for review workflows — only after #4 reorder proves the fail-fast shape (#4).
10. Frontend artifact handoff review → deploy (#6).

**Caller wave (largest residual org saving):**
11. `paths-ignore` on every PR-triggered caller workflow (#5).
12. Finish `security.yml` → `access-analyzer-check.yml` migration (removes duplicated setup; secondary minute win).

---

## Rough residual budget

| Bucket | Est. recoverable min/mo |
|---|---|
| Shared-workflow fixes (#1–4, #6–8) | ~100–250 |
| Caller path filters (#5) | ~150–400 |
| **Total remaining** | **~250–650** of the ~2,250 baseline |

July already removed the concurrency / triple-synth / always-on log-shipping tax. The leftover is mostly cache correctness, one redundant install, optional Docker setup, serial fail-slow ordering, and caller-side path filters.
