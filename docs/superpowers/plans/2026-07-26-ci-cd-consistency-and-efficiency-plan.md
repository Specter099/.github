# CI/CD Consistency & Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the shared-workflow half of `docs/superpowers/specs/2026-07-26-ci-cd-consistency-and-efficiency-design.md` (merged in [#139](https://github.com/Specter099/.github/pull/139)) in `Specter099/.github` itself — the 20-repo caller migration is a separate follow-up plan, not this one.

**Architecture:** Twelve `.github/workflows/*.yml` files get targeted edits (concurrency, synth-reuse, timeouts, secrets declarations, ci-logs default) plus one new composite action (`actions/log-metadata`) that de-duplicates a block currently copy-pasted five times. Every change must keep `./scripts/local-ci.sh` green — this repo has its own enforcement gate (`scripts/check_workflow_invariants.py`, checks WF001–WF014, tracked via `.github/workflow-invariants-baseline.yml`) that was merged in [#138](https://github.com/Specter099/.github/pull/138) *after* the design spec was written. Several of this plan's fixes directly resolve findings that gate already has baselined (tracked-but-unfixed) — those baseline entries must be removed as each fix lands, per that file's own convention ("removing an entry is how you claim the fix").

**Tech Stack:** GitHub Actions YAML (`workflow_call` reusable workflows, composite actions), Python 3.12 (`scripts/check_workflow_invariants.py`), bash (`scripts/local-ci.sh`).

**One addition beyond the merged spec:** `cdk-review.yml`/`static-site-review.yml` are missing an `environment:` key on their job. `README.md`/`CLAUDE.md` document `AWS_ROLE_ARN` as an **environment** secret on `production` — but environment secrets only resolve for jobs that declare `environment:`. Without it, `secrets.AWS_ROLE_ARN` is empty under the documented setup, every AWS-gated step is skipped, and the job still exits 0 — a required status check that silently checked nothing. This is directly load-bearing for this plan's premise (branch protection as a real CI gate), so it's in scope. Everything else in `docs/reviews/2026-07-25-actions-security-efficiency-review.md` beyond what's listed in tasks below (credential scoping/S1, internal-action tag-pinning/S6, hash-locked tool installs/S8, parallel job split/E5, and all Low-severity items) stays out of scope — already tracked in that review + the baseline file for separate future work.

**Execution choice:** Inline execution (this session, via `executing-plans`) rather than subagent-driven — every task touches a small set of files I've already read line-for-line this session, and spinning up fresh subagents with no shared context would be slower here than in-session edits with the existing invariants gate as the safety net.

---

## Before you start

Run this once to see the current gate state:

```bash
./scripts/local-ci.sh
```

Expected: `PASS`. This is the non-strict run — it applies the baseline, so the 25 pre-existing accepted findings (WF004 ×12, WF005 ×4, WF007 ×2, WF008 ×4, WF011 ×2, WF012 ×1) are suppressed and don't fail the gate.

**`--strict` is a different, harsher command — never expect `PASS` from it in this plan.** It ignores the baseline entirely (`local-ci.sh --help`: "ignore the baseline and show every finding"), so it always fails today (`FAIL`, 5 blocking: WF005 ×4 + WF012 ×1 are `error`-severity and surface unbaselined) and will still fail after every task in this plan, because WF012 (README doc drift) is explicitly out of scope and never gets a baseline removal. Use `--strict` only to *inspect* which findings exist, grep'd to the specific check/file you just touched — never as a pass/fail gate. The actual gate to satisfy, in every task and at the end, is plain `./scripts/local-ci.sh` (no flag).

---

### Task 1: New composite action `actions/log-metadata`

**Files:**
- Create: `.github/actions/log-metadata/action.yml`

The "Generate log metadata" step is byte-for-byte identical (modulo the log-dir path) in `cdk-review.yml:271-300`, `cdk-deploy.yml:164-193`, `python-ci.yml:241-270`, `static-site-review.yml:233-262`, `static-site-deploy.yml:182-211`. Extract it once; each of those five files gets a one-step swap in its own task below.

- [ ] **Step 1: Create the composite action**

```yaml
name: Generate CI Log Metadata
description: >
  Write a metadata.json describing this workflow run into the CI log
  directory, for ship-logs to upload alongside the step logs.

inputs:
  log-dir:
    description: Directory to write metadata.json into (created if absent)
    required: true

runs:
  using: composite
  steps:
    - name: Generate log metadata
      shell: bash
      env:
        LOG_DIR: ${{ inputs.log-dir }}
        META_REPOSITORY: ${{ github.repository }}
        META_WORKFLOW: ${{ github.workflow }}
        META_RUN_ID: ${{ github.run_id }}
        META_RUN_ATTEMPT: ${{ github.run_attempt }}
        META_REF: ${{ github.ref }}
        META_SHA: ${{ github.sha }}
        META_ACTOR: ${{ github.actor }}
        META_EVENT: ${{ github.event_name }}
        META_JOB_STATUS: ${{ job.status }}
      run: |
        mkdir -p "$LOG_DIR"
        python3 -c "
        import json, os, datetime
        meta = {
            'repository': os.environ['META_REPOSITORY'],
            'workflow': os.environ['META_WORKFLOW'],
            'run_id': os.environ['META_RUN_ID'],
            'run_attempt': os.environ['META_RUN_ATTEMPT'],
            'ref': os.environ['META_REF'],
            'sha': os.environ['META_SHA'],
            'actor': os.environ['META_ACTOR'],
            'event': os.environ['META_EVENT'],
            'job_status': os.environ['META_JOB_STATUS'],
            'timestamp': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        with open(os.path.join(os.environ['LOG_DIR'], 'metadata.json'), 'w') as f:
            json.dump(meta, f, indent=2)
        "
```

- [ ] **Step 2: Verify it's picked up by the gate**

Run: `yamllint -c .yamllint.yml .github/actions/log-metadata/action.yml`
Expected: no output (clean).

Run: `python scripts/check_workflow_invariants.py --strict --path . 2>&1 | grep log-metadata`
Expected: no output — a composite action has no `jobs:`, so WF001/WF002/WF008/WF010 don't apply to it, and it has no `uses:` steps, so WF003/WF004 don't either.

- [ ] **Step 3: Commit**

```bash
git add .github/actions/log-metadata/action.yml
git commit -m "feat: add log-metadata composite action

Extracts the 'generate log metadata' Python block that's currently
copy-pasted in five workflows into a single composite action."
```

---

### Task 2: `cdk-review.yml` — environment fix, synth dedupe, concurrency, ci-logs default, timeout

**Files:**
- Modify: `.github/workflows/cdk-review.yml`

- [ ] **Step 1: Add the `environment` input**

Find (in the `inputs:` block, right after `aws-region`):
```yaml
      aws-region:
        description: AWS region to deploy to
        type: string
        default: "us-east-1"
      cdk-version:
```
Replace with:
```yaml
      aws-region:
        description: AWS region to deploy to
        type: string
        default: "us-east-1"
      environment:
        description: GitHub environment to use (controls which secrets/vars are loaded)
        type: string
        default: "production"
      cdk-version:
```

- [ ] **Step 2: Add `environment:` and `concurrency:` to the job, tighten the timeout**

Find:
```yaml
jobs:
  review:
    name: Synth & Diff
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    env:
      AWS_ROLE_ARN: ${{ secrets.AWS_ROLE_ARN }}
    steps:
```
Replace with:
```yaml
jobs:
  review:
    name: Synth & Diff
    runs-on: ubuntu-latest
    timeout-minutes: 10
    concurrency:
      group: cdk-review-${{ github.ref }}
      cancel-in-progress: true
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    environment: ${{ inputs.environment }}
    env:
      AWS_ROLE_ARN: ${{ secrets.AWS_ROLE_ARN }}
    steps:
```

- [ ] **Step 3: Isolate the CDK Nag synth to its own output directory**

This must land *before* Step 4 — it's what makes reusing `cdk.out` for the diff safe. Today, `CDK_NAG=true cdk synth` (no `--output` flag) writes to the same default `cdk.out/` directory as the plain synth earlier in the job, silently overwriting it with a Nag-contaminated assembly (`NagSuppressions.add_resource_suppressions_by_path` embeds `Metadata.cdk_nag` blocks into the template). Giving the Nag synth its own output directory means `cdk.out/` always holds the plain assembly, regardless of step order.

Find:
```yaml
          if ! CDK_NAG=true cdk synth > "$RUNNER_TEMP/cdk-nag-synth.log" 2>&1; then
```
Replace with:
```yaml
          if ! CDK_NAG=true cdk synth --output cdk-nag.out > "$RUNNER_TEMP/cdk-nag-synth.log" 2>&1; then
```

- [ ] **Step 4: Reuse the plain synth's `cdk.out` for the diff**

Find:
```yaml
      - name: CDK Diff
        if: env.AWS_ROLE_ARN != ''
        id: diff
        env:
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          diff_output=$(cdk diff 2>&1) || true
```
Replace with:
```yaml
      - name: CDK Diff
        if: env.AWS_ROLE_ARN != ''
        id: diff
        env:
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          # Reuses the cdk.out from the "CDK Synth" step above instead of
          # letting `cdk diff` re-synthesize — cuts this job from 3 synths to
          # 2. Not pointed at the CDK Nag synth's assembly: that one writes
          # to cdk-nag.out (see the CDK Nag step) specifically so it can't
          # contaminate this one with cdk_nag Metadata.
          diff_output=$(cdk diff --app cdk.out 2>&1) || true
```

- [ ] **Step 5: Flip the `enable-ci-logs` default**

Find:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: true
```
Replace with:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: false
```

- [ ] **Step 6: Swap the inline log-metadata step for the composite action**

Find:
```yaml
      - name: Generate log metadata
        if: always() && inputs.enable-ci-logs
        env:
          META_REPOSITORY: ${{ github.repository }}
          META_WORKFLOW: ${{ github.workflow }}
          META_RUN_ID: ${{ github.run_id }}
          META_RUN_ATTEMPT: ${{ github.run_attempt }}
          META_REF: ${{ github.ref }}
          META_SHA: ${{ github.sha }}
          META_ACTOR: ${{ github.actor }}
          META_EVENT: ${{ github.event_name }}
          META_JOB_STATUS: ${{ job.status }}
        run: |
          python3 -c "
          import json, os, datetime
          meta = {
              'repository': os.environ['META_REPOSITORY'],
              'workflow': os.environ['META_WORKFLOW'],
              'run_id': os.environ['META_RUN_ID'],
              'run_attempt': os.environ['META_RUN_ATTEMPT'],
              'ref': os.environ['META_REF'],
              'sha': os.environ['META_SHA'],
              'actor': os.environ['META_ACTOR'],
              'event': os.environ['META_EVENT'],
              'job_status': os.environ['META_JOB_STATUS'],
              'timestamp': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
          }
          with open(os.path.join(os.environ['RUNNER_TEMP'], 'ci-logs', 'metadata.json'), 'w') as f:
              json.dump(meta, f, indent=2)
          "
```
Replace with:
```yaml
      - name: Generate log metadata
        if: always() && inputs.enable-ci-logs
        uses: Specter099/.github/.github/actions/log-metadata@main
        with:
          log-dir: ${{ runner.temp }}/ci-logs
```

- [ ] **Step 7: Verify**

Run: `python scripts/check_workflow_invariants.py --strict 2>&1 | grep "cdk-review.yml"`
Expected: no `WF008` line for `cdk-review.yml` (fixed by the concurrency block; `cdk-review.yml` never had a WF005 finding, so none should appear either — `--strict` here is just for inspecting a specific finding class, not a pass/fail gate; see "Before you start").

Run: `yamllint -c .yamllint.yml .github/workflows/cdk-review.yml && actionlint .github/workflows/cdk-review.yml`
Expected: both clean.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/cdk-review.yml
git commit -m "fix(cdk-review): add environment input, dedupe synth, add concurrency

- Add environment input (default production) so the documented
  environment-secret setup for AWS_ROLE_ARN actually resolves — without
  it every AWS-gated step silently skips and the job still passes.
- Isolate the CDK Nag synth to cdk-nag.out and reuse the plain synth's
  cdk.out for cdk diff, cutting 3 synths per run to 2.
- Add job-level concurrency so superseded PR pushes cancel in flight.
- Flip enable-ci-logs default to false (opt-in).
- Tighten timeout-minutes 20 -> 10.
- Use the new log-metadata composite action instead of inlining it."
```

---

### Task 3: `cdk-deploy.yml` — secrets block, synth reuse, smoke-test hard-fail, ci-logs default, timeout

**Files:**
- Modify: `.github/workflows/cdk-deploy.yml`

- [ ] **Step 1: Add the `smoke-test-required` input**

Find:
```yaml
      smoke-test-url:
        description: URL to curl after deploy for smoke test (optional)
        type: string
        default: ""
      enable-ci-logs:
```
Replace with:
```yaml
      smoke-test-url:
        description: URL to curl after deploy for smoke test (optional)
        type: string
        default: ""
      smoke-test-required:
        description: >-
          Fail the job when the smoke test returns a bad/unreachable status.
          Set false for repos with known post-deploy propagation delay
          (e.g. CloudFront) where a warn-only smoke test is preferred.
        type: boolean
        default: true
      enable-ci-logs:
```

- [ ] **Step 2: Flip the `enable-ci-logs` default**

Find:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: true
```
Replace with:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: false
```

- [ ] **Step 3: Declare the `AWS_ROLE_ARN` secret explicitly**

Find (the end of the `inputs:` block, right before `jobs:`):
```yaml
      ci-log-cloudwatch-group:
        description: >-
          CloudWatch Logs group name. Empty/unset falls back to
          `vars.CI_LOGS_LOG_GROUP` in the calling repo. Required when
          ci-log-destination includes 'cloudwatch'.
        type: string
        default: ""
jobs:
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
    timeout-minutes: 45
```
Replace with:
```yaml
      ci-log-cloudwatch-group:
        description: >-
          CloudWatch Logs group name. Empty/unset falls back to
          `vars.CI_LOGS_LOG_GROUP` in the calling repo. Required when
          ci-log-destination includes 'cloudwatch'.
        type: string
        default: ""
    secrets:
      AWS_ROLE_ARN:
        description: IAM role ARN for OIDC federation
        required: true
jobs:
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
    timeout-minutes: 15
```

- [ ] **Step 4: Insert a pre-deploy synth and reuse it in `cdk deploy`**

Find:
```yaml
      - name: CDK Deploy
        env:
          CDK_STACKS: ${{ inputs.stacks }}
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          # Guard caller-controlled input before unquoted expansion: only
          # '--all' or space-separated stack names are accepted, so flags or
          # shell metacharacters can't be smuggled into the cdk command.
          if [[ ! "$CDK_STACKS" =~ ^(--all|[A-Za-z0-9_-]+( [A-Za-z0-9_-]+)*)$ ]]; then
            echo "::error::invalid stacks input '$CDK_STACKS' — must be '--all' or space-separated stack names"
            exit 1
          fi
          if [ "$ENABLE_LOGS" = "true" ]; then
            cdk deploy $CDK_STACKS --require-approval never --verbose --outputs-file outputs.json 2>&1 \
              | awk '{ print strftime("%Y-%m-%dT%H:%M:%SZ"), $0; fflush() }' \
              | tee "$RUNNER_TEMP/ci-logs/cdk-deploy.log"
          else
            cdk deploy $CDK_STACKS --require-approval never --verbose --outputs-file outputs.json
          fi
```
Replace with:
```yaml
      - name: CDK Synth
        env:
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          if [ "$ENABLE_LOGS" = "true" ]; then
            cdk synth 2>&1 | tee "$RUNNER_TEMP/ci-logs/cdk-synth.log"
          else
            cdk synth
          fi

      - name: CDK Deploy
        env:
          CDK_STACKS: ${{ inputs.stacks }}
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          # Guard caller-controlled input before unquoted expansion: only
          # '--all' or space-separated stack names are accepted, so flags or
          # shell metacharacters can't be smuggled into the cdk command.
          if [[ ! "$CDK_STACKS" =~ ^(--all|[A-Za-z0-9_-]+( [A-Za-z0-9_-]+)*)$ ]]; then
            echo "::error::invalid stacks input '$CDK_STACKS' — must be '--all' or space-separated stack names"
            exit 1
          fi
          # --app cdk.out reuses the "CDK Synth" step above instead of
          # letting `cdk deploy` re-synthesize from scratch.
          if [ "$ENABLE_LOGS" = "true" ]; then
            cdk deploy $CDK_STACKS --app cdk.out --require-approval never --verbose --outputs-file outputs.json 2>&1 \
              | awk '{ print strftime("%Y-%m-%dT%H:%M:%SZ"), $0; fflush() }' \
              | tee "$RUNNER_TEMP/ci-logs/cdk-deploy.log"
          else
            cdk deploy $CDK_STACKS --app cdk.out --require-approval never --verbose --outputs-file outputs.json
          fi
```

- [ ] **Step 5: Make the smoke test fail the job on a bad status**

Find:
```yaml
      - name: Smoke test
        if: ${{ inputs.smoke-test-url != '' }}
        env:
          SMOKE_TEST_URL: ${{ inputs.smoke-test-url }}
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          _run_smoke() {
            status=$(curl -o /dev/null -s -w "%{http_code}" --max-time 15 \
              "$SMOKE_TEST_URL" || echo "000")
            echo "Smoke test HTTP status: $status"
            echo "## Smoke Test" >> "$GITHUB_STEP_SUMMARY"
            echo "Response from \`$SMOKE_TEST_URL\`: **HTTP $status**" >> "$GITHUB_STEP_SUMMARY"
            if [[ "$status" == "000" ]]; then
              echo "::warning::Smoke test: curl failed or timed out"
            else
              echo "Smoke test passed — endpoint is reachable."
            fi
          }
          if [ "$ENABLE_LOGS" = "true" ]; then
            _run_smoke 2>&1 \
              | awk '{ print strftime("%Y-%m-%dT%H:%M:%SZ"), $0; fflush() }' \
              | tee "$RUNNER_TEMP/ci-logs/smoke-test.log"
          else
            _run_smoke
          fi
```
Replace with:
```yaml
      - name: Smoke test
        if: ${{ inputs.smoke-test-url != '' }}
        env:
          SMOKE_TEST_URL: ${{ inputs.smoke-test-url }}
          SMOKE_TEST_REQUIRED: ${{ inputs.smoke-test-required }}
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          _run_smoke() {
            status=$(curl -o /dev/null -s -w "%{http_code}" --max-time 15 \
              "$SMOKE_TEST_URL" || echo "000")
            echo "Smoke test HTTP status: $status"
            echo "## Smoke Test" >> "$GITHUB_STEP_SUMMARY"
            echo "Response from \`$SMOKE_TEST_URL\`: **HTTP $status**" >> "$GITHUB_STEP_SUMMARY"
            if [[ "$status" == "000" || "$status" -ge "400" ]]; then
              if [ "$SMOKE_TEST_REQUIRED" = "true" ]; then
                echo "::error::Smoke test failed: HTTP $status from $SMOKE_TEST_URL"
                return 1
              else
                echo "::warning::Smoke test failed (non-blocking, smoke-test-required is false): HTTP $status"
              fi
            else
              echo "Smoke test passed — endpoint is reachable."
            fi
          }
          if [ "$ENABLE_LOGS" = "true" ]; then
            _run_smoke 2>&1 \
              | awk '{ print strftime("%Y-%m-%dT%H:%M:%SZ"), $0; fflush() }' \
              | tee "$RUNNER_TEMP/ci-logs/smoke-test.log"
          else
            _run_smoke
          fi
```

- [ ] **Step 6: Swap the inline log-metadata step for the composite action**

Find the same "Generate log metadata" block as Task 2 Step 6 (identical text, this file's copy) and replace with:
```yaml
      - name: Generate log metadata
        if: always() && inputs.enable-ci-logs
        uses: Specter099/.github/.github/actions/log-metadata@main
        with:
          log-dir: ${{ runner.temp }}/ci-logs
```

- [ ] **Step 7: Verify**

Run: `python scripts/check_workflow_invariants.py --strict 2>&1 | grep "cdk-deploy.yml"`
Expected: no `WF005` line for `cdk-deploy.yml` (the secrets block fix resolves it). The baseline still has a now-stale entry for it until Task 12 removes it — that's expected at this point in the plan, not a bug.

Run: `yamllint -c .yamllint.yml .github/workflows/cdk-deploy.yml && actionlint .github/workflows/cdk-deploy.yml`
Expected: both clean.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/cdk-deploy.yml
git commit -m "fix(cdk-deploy): declare secrets, reuse synth, hard-fail smoke test

- Declare workflow_call.secrets.AWS_ROLE_ARN explicitly instead of
  relying on implicit secrets: inherit.
- Add a pre-deploy cdk synth, reused via --app cdk.out in cdk deploy
  instead of letting deploy re-synthesize from scratch.
- Smoke test now fails the job on a bad/unreachable status (new
  smoke-test-required input, default true, for opting back to warn-only).
- Flip enable-ci-logs default to false.
- Tighten timeout-minutes 45 -> 15.
- Use the new log-metadata composite action instead of inlining it."
```

---

### Task 4: `static-site-review.yml` — environment fix, synth reuse, concurrency, ci-logs default, timeout

**Files:**
- Modify: `.github/workflows/static-site-review.yml`

- [ ] **Step 1: Add the `environment` input**

Find:
```yaml
      aws-region:
        description: AWS region
        type: string
        default: "us-east-1"
      cdk-version:
```
Replace with:
```yaml
      aws-region:
        description: AWS region
        type: string
        default: "us-east-1"
      environment:
        description: GitHub environment to use (controls which secrets/vars are loaded)
        type: string
        default: "production"
      cdk-version:
```

- [ ] **Step 2: Add `environment:` and `concurrency:` to the job, tighten the timeout**

Find:
```yaml
jobs:
  review:
    name: Lint, Test & Diff
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    steps:
```
Replace with:
```yaml
jobs:
  review:
    name: Lint, Test & Diff
    runs-on: ubuntu-latest
    timeout-minutes: 10
    concurrency:
      group: static-site-review-${{ github.ref }}
      cancel-in-progress: true
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    environment: ${{ inputs.environment }}
    steps:
```

- [ ] **Step 3: Reuse the synth for the diff**

This file has no CDK Nag step (unlike `cdk-review.yml`), so there's nothing else writing to `cdk.out` — just the one synth.

Find:
```yaml
      - name: CDK Diff
        id: diff
        working-directory: ${{ inputs.infra-dir }}
        env:
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          diff_output=$(cdk diff 2>&1) || true
```
Replace with:
```yaml
      - name: CDK Diff
        id: diff
        working-directory: ${{ inputs.infra-dir }}
        env:
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          # Reuses the cdk.out from the "CDK Synth" step above instead of
          # letting `cdk diff` re-synthesize.
          diff_output=$(cdk diff --app cdk.out 2>&1) || true
```

- [ ] **Step 4: Flip the `enable-ci-logs` default**

Find:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: true
```
Replace with:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: false
```

- [ ] **Step 5: Swap the inline log-metadata step for the composite action**

Find the same "Generate log metadata" block as Task 2 Step 6 (this file's copy) and replace with:
```yaml
      - name: Generate log metadata
        if: always() && inputs.enable-ci-logs
        uses: Specter099/.github/.github/actions/log-metadata@main
        with:
          log-dir: ${{ runner.temp }}/ci-logs
```

- [ ] **Step 6: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/static-site-review.yml && actionlint .github/workflows/static-site-review.yml`
Expected: both clean.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/static-site-review.yml
git commit -m "fix(static-site-review): add environment input, reuse synth, add concurrency

Mirrors the cdk-review.yml fixes: environment input so AWS_ROLE_ARN
resolves under the documented environment-secret setup, cdk diff
reuses the existing synth, job-level concurrency, ci-logs default
false, tighter timeout, and the log-metadata composite action."
```

---

### Task 5: `static-site-deploy.yml` — stacks/secrets/smoke-test parity, synth reuse, ci-logs default, timeout

**Files:**
- Modify: `.github/workflows/static-site-deploy.yml`

- [ ] **Step 1: Add the `stacks` input (parity with `cdk-deploy.yml`)**

Find:
```yaml
      infra-dir:
        description: Path to the CDK infra directory (contains app.py)
        type: string
        required: true
      aws-region:
        description: AWS region
        type: string
        default: "us-east-1"
```
Replace with:
```yaml
      infra-dir:
        description: Path to the CDK infra directory (contains app.py)
        type: string
        required: true
      stacks:
        description: Stack name(s) to deploy, or '--all' for all stacks
        type: string
        default: "--all"
      aws-region:
        description: AWS region
        type: string
        default: "us-east-1"
```

- [ ] **Step 2: Add the `smoke-test-required` input**

Find:
```yaml
      smoke-test-url:
        description: URL to curl after deploy for smoke test (optional)
        type: string
        default: ""
      enable-ci-logs:
```
Replace with:
```yaml
      smoke-test-url:
        description: URL to curl after deploy for smoke test (optional)
        type: string
        default: ""
      smoke-test-required:
        description: >-
          Fail the job when the smoke test returns a bad/unreachable status.
          Set false for repos with known post-deploy propagation delay
          (e.g. CloudFront) where a warn-only smoke test is preferred.
        type: boolean
        default: true
      enable-ci-logs:
```

- [ ] **Step 3: Flip the `enable-ci-logs` default**

Find:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: true
```
Replace with:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: false
```

- [ ] **Step 4: Declare the `AWS_ROLE_ARN` secret explicitly**

Find (end of the `inputs:` block, right before `jobs:`):
```yaml
      ci-log-cloudwatch-group:
        description: >-
          CloudWatch Logs group name. Empty/unset falls back to
          `vars.CI_LOGS_LOG_GROUP` in the calling repo. Required when
          ci-log-destination includes 'cloudwatch'.
        type: string
        default: ""

jobs:
  deploy:
    name: Build & Deploy
    runs-on: ubuntu-latest
    timeout-minutes: 45
```
Replace with:
```yaml
      ci-log-cloudwatch-group:
        description: >-
          CloudWatch Logs group name. Empty/unset falls back to
          `vars.CI_LOGS_LOG_GROUP` in the calling repo. Required when
          ci-log-destination includes 'cloudwatch'.
        type: string
        default: ""
    secrets:
      AWS_ROLE_ARN:
        description: IAM role ARN for OIDC federation
        required: true

jobs:
  deploy:
    name: Build & Deploy
    runs-on: ubuntu-latest
    timeout-minutes: 20
```

- [ ] **Step 5: Insert a pre-deploy synth (after QEMU/Buildx setup) and target stacks**

The new synth step must land *after* "Set up Docker Buildx" (Docker-based asset bundling needs QEMU/Buildx already configured) and immediately before "CDK Deploy".

Find:
```yaml
      - name: CDK Deploy
        working-directory: ${{ inputs.infra-dir }}
        env:
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          if [ "$ENABLE_LOGS" = "true" ]; then
            cdk deploy --require-approval never --outputs-file outputs.json 2>&1 | tee "$RUNNER_TEMP/ci-logs/cdk-deploy.log"
          else
            cdk deploy --require-approval never --outputs-file outputs.json
          fi
```
Replace with:
```yaml
      - name: CDK Synth
        working-directory: ${{ inputs.infra-dir }}
        env:
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          if [ "$ENABLE_LOGS" = "true" ]; then
            cdk synth 2>&1 | tee "$RUNNER_TEMP/ci-logs/cdk-synth.log"
          else
            cdk synth
          fi

      - name: CDK Deploy
        working-directory: ${{ inputs.infra-dir }}
        env:
          CDK_STACKS: ${{ inputs.stacks }}
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          # Guard caller-controlled input before unquoted expansion: only
          # '--all' or space-separated stack names are accepted, so flags or
          # shell metacharacters can't be smuggled into the cdk command.
          if [[ ! "$CDK_STACKS" =~ ^(--all|[A-Za-z0-9_-]+( [A-Za-z0-9_-]+)*)$ ]]; then
            echo "::error::invalid stacks input '$CDK_STACKS' — must be '--all' or space-separated stack names"
            exit 1
          fi
          # --app cdk.out reuses the "CDK Synth" step above.
          if [ "$ENABLE_LOGS" = "true" ]; then
            cdk deploy $CDK_STACKS --app cdk.out --require-approval never --outputs-file outputs.json 2>&1 | tee "$RUNNER_TEMP/ci-logs/cdk-deploy.log"
          else
            cdk deploy $CDK_STACKS --app cdk.out --require-approval never --outputs-file outputs.json
          fi
```

- [ ] **Step 6: Make the smoke test fail the job on a bad status**

Find:
```yaml
      - name: Smoke test
        if: ${{ inputs.smoke-test-url != '' }}
        env:
          SMOKE_TEST_URL: ${{ inputs.smoke-test-url }}
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          _run_smoke() {
            status=$(curl -o /dev/null -s -w "%{http_code}" --max-time 15 \
              "$SMOKE_TEST_URL" || echo "000")
            echo "Smoke test HTTP status: $status"
            {
              echo "## Smoke Test"
              echo "Response from \`$SMOKE_TEST_URL\`: **HTTP $status**"
            } >> "$GITHUB_STEP_SUMMARY"
            if [[ "$status" == "000" ]]; then
              echo "::warning::Smoke test: curl failed or timed out"
            else
              echo "Smoke test passed — endpoint is reachable."
            fi
          }
          if [ "$ENABLE_LOGS" = "true" ]; then
            _run_smoke 2>&1 | tee "$RUNNER_TEMP/ci-logs/smoke-test.log"
          else
            _run_smoke
          fi
```
Replace with:
```yaml
      - name: Smoke test
        if: ${{ inputs.smoke-test-url != '' }}
        env:
          SMOKE_TEST_URL: ${{ inputs.smoke-test-url }}
          SMOKE_TEST_REQUIRED: ${{ inputs.smoke-test-required }}
          ENABLE_LOGS: ${{ inputs.enable-ci-logs }}
        run: |
          set -o pipefail
          _run_smoke() {
            status=$(curl -o /dev/null -s -w "%{http_code}" --max-time 15 \
              "$SMOKE_TEST_URL" || echo "000")
            echo "Smoke test HTTP status: $status"
            {
              echo "## Smoke Test"
              echo "Response from \`$SMOKE_TEST_URL\`: **HTTP $status**"
            } >> "$GITHUB_STEP_SUMMARY"
            if [[ "$status" == "000" || "$status" -ge "400" ]]; then
              if [ "$SMOKE_TEST_REQUIRED" = "true" ]; then
                echo "::error::Smoke test failed: HTTP $status from $SMOKE_TEST_URL"
                return 1
              else
                echo "::warning::Smoke test failed (non-blocking, smoke-test-required is false): HTTP $status"
              fi
            else
              echo "Smoke test passed — endpoint is reachable."
            fi
          }
          if [ "$ENABLE_LOGS" = "true" ]; then
            _run_smoke 2>&1 | tee "$RUNNER_TEMP/ci-logs/smoke-test.log"
          else
            _run_smoke
          fi
```

- [ ] **Step 7: Swap the inline log-metadata step for the composite action**

Find the same "Generate log metadata" block as Task 2 Step 6 (this file's copy) and replace with:
```yaml
      - name: Generate log metadata
        if: always() && inputs.enable-ci-logs
        uses: Specter099/.github/.github/actions/log-metadata@main
        with:
          log-dir: ${{ runner.temp }}/ci-logs
```

- [ ] **Step 8: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/static-site-deploy.yml && actionlint .github/workflows/static-site-deploy.yml`
Expected: both clean.

- [ ] **Step 9: Commit**

```bash
git add .github/workflows/static-site-deploy.yml
git commit -m "fix(static-site-deploy): add stacks input, declare secrets, reuse synth

- Add a stacks input (default --all), closing the parity gap with
  cdk-deploy.yml.
- Declare workflow_call.secrets.AWS_ROLE_ARN explicitly.
- Add a pre-deploy cdk synth (after QEMU/Buildx setup), reused via
  --app cdk.out in cdk deploy.
- Smoke test now fails the job on a bad/unreachable status (new
  smoke-test-required input, default true).
- Flip enable-ci-logs default to false.
- Tighten timeout-minutes 45 -> 20.
- Use the new log-metadata composite action instead of inlining it."
```

---

### Task 6: `python-ci.yml` — matrix-aware concurrency, ci-logs default, timeout

**Files:**
- Modify: `.github/workflows/python-ci.yml`

- [ ] **Step 1: Update the header comment and add job-level concurrency**

Find:
```yaml
name: Python CI
# Requires callers to include ruff and pytest in their requirements file.
# Install via requirements-path (default) or override with install-command.
#
# Callers should set concurrency at the workflow level:
#   concurrency:
#     group: python-ci-${{ github.ref }}
#     cancel-in-progress: true
```
Replace with:
```yaml
name: Python CI
# Requires callers to include ruff and pytest in their requirements file.
# Install via requirements-path (default) or override with install-command.
#
# Concurrency is handled by this workflow's own job — callers don't need to
# set it themselves.
```

Find:
```yaml
jobs:
  ci:
    name: "Lint, Secrets & Test (py${{ matrix.python-version }})"
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
```
Replace with:
```yaml
jobs:
  ci:
    name: "Lint, Secrets & Test (py${{ matrix.python-version }})"
    runs-on: ubuntu-latest
    timeout-minutes: 12
    concurrency:
      group: python-ci-${{ github.ref }}-${{ matrix.python-version }}
      cancel-in-progress: true
    permissions:
```

The group key **includes `matrix.python-version`** — omitting it would make the matrix's own parallel legs (e.g. `3.11` and `3.12`) cancel each other instead of running side by side.

- [ ] **Step 2: Flip the `enable-ci-logs` default**

Find:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: true
```
Replace with:
```yaml
      enable-ci-logs:
        description: Ship detailed step logs to S3/CloudWatch
        type: boolean
        default: false
```

- [ ] **Step 3: Swap the inline log-metadata step for the composite action**

This file's copy is gated on an extra condition (`&& env.AWS_ROLE_ARN != ''`) that the other four files' copies don't have — the `if:` line differs from Task 2 Step 6's block, so it needs its own literal Find.

Find:
```yaml
      - name: Generate log metadata
        if: always() && inputs.enable-ci-logs && env.AWS_ROLE_ARN != ''
        env:
          META_REPOSITORY: ${{ github.repository }}
          META_WORKFLOW: ${{ github.workflow }}
          META_RUN_ID: ${{ github.run_id }}
          META_RUN_ATTEMPT: ${{ github.run_attempt }}
          META_REF: ${{ github.ref }}
          META_SHA: ${{ github.sha }}
          META_ACTOR: ${{ github.actor }}
          META_EVENT: ${{ github.event_name }}
          META_JOB_STATUS: ${{ job.status }}
        run: |
          python3 -c "
          import json, os, datetime
          meta = {
              'repository': os.environ['META_REPOSITORY'],
              'workflow': os.environ['META_WORKFLOW'],
              'run_id': os.environ['META_RUN_ID'],
              'run_attempt': os.environ['META_RUN_ATTEMPT'],
              'ref': os.environ['META_REF'],
              'sha': os.environ['META_SHA'],
              'actor': os.environ['META_ACTOR'],
              'event': os.environ['META_EVENT'],
              'job_status': os.environ['META_JOB_STATUS'],
              'timestamp': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
          }
          with open(os.path.join(os.environ['RUNNER_TEMP'], 'ci-logs', 'metadata.json'), 'w') as f:
              json.dump(meta, f, indent=2)
          "
```
Replace with:
```yaml
      - name: Generate log metadata
        if: always() && inputs.enable-ci-logs && env.AWS_ROLE_ARN != ''
        uses: Specter099/.github/.github/actions/log-metadata@main
        with:
          log-dir: ${{ runner.temp }}/ci-logs
```

- [ ] **Step 4: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/python-ci.yml && actionlint .github/workflows/python-ci.yml`
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/python-ci.yml
git commit -m "fix(python-ci): add matrix-aware concurrency, flip ci-logs default

Concurrency group includes matrix.python-version so matrix legs don't
cancel each other. Removes the comment-only convention that asked
callers to set this themselves — it was never actually enforced.
Flips enable-ci-logs default to false; tightens timeout 20 -> 12."
```

---

### Task 7: `self-test.yml` — concurrency, timeout

**Files:**
- Modify: `.github/workflows/self-test.yml`

- [ ] **Step 1: Add concurrency and tighten the timeout**

Find:
```yaml
jobs:
  test:
    name: Lint & Test
    runs-on: ubuntu-latest
    timeout-minutes: 15
    permissions:
      contents: read
    steps:
```
Replace with:
```yaml
jobs:
  test:
    name: Lint & Test
    runs-on: ubuntu-latest
    timeout-minutes: 8
    concurrency:
      group: self-test-${{ github.ref }}
      cancel-in-progress: true
    permissions:
      contents: read
    steps:
```

- [ ] **Step 2: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/self-test.yml && actionlint .github/workflows/self-test.yml`
Expected: both clean.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/self-test.yml
git commit -m "fix(self-test): add concurrency group, tighten timeout 15 -> 8"
```

---

### Task 8: `repo-backup.yml` — declare the secret, tighten timeout

**Files:**
- Modify: `.github/workflows/repo-backup.yml`

This must land before Task 9 (`backup.yml`) — Task 9's rewrite passes `secrets: { AWS_ROLE_ARN: ... }` to a `workflow_call` that only accepts a declared secret once this task adds it.

- [ ] **Step 1: Declare `AWS_ROLE_ARN` under `workflow_call`**

Find:
```yaml
on:
  workflow_call:
    inputs:
      s3-bucket:
        description: S3 bucket name to upload the backup to
        type: string
        required: true
      s3-prefix:
        description: Key prefix (folder) within the bucket (defaults to repo name)
        type: string
        default: ""
      aws-region:
        description: AWS region of the S3 bucket
        type: string
        default: "us-east-1"
      environment:
        description: GitHub environment whose secrets contain AWS_ROLE_ARN
        type: string
        default: "backup"
  workflow_dispatch:
```
Replace with:
```yaml
on:
  workflow_call:
    inputs:
      s3-bucket:
        description: S3 bucket name to upload the backup to
        type: string
        required: true
      s3-prefix:
        description: Key prefix (folder) within the bucket (defaults to repo name)
        type: string
        default: ""
      aws-region:
        description: AWS region of the S3 bucket
        type: string
        default: "us-east-1"
      environment:
        description: GitHub environment whose secrets contain AWS_ROLE_ARN
        type: string
        default: "backup"
    secrets:
      AWS_ROLE_ARN:
        description: IAM role ARN for OIDC federation
        required: true
  workflow_dispatch:
```

- [ ] **Step 2: Tighten the timeout**

Find:
```yaml
jobs:
  backup:
    name: Backup to S3
    runs-on: ubuntu-latest
    timeout-minutes: 15
    environment: ${{ inputs.environment }}
```
Replace with:
```yaml
jobs:
  backup:
    name: Backup to S3
    runs-on: ubuntu-latest
    timeout-minutes: 8
    environment: ${{ inputs.environment }}
```

- [ ] **Step 3: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/repo-backup.yml && actionlint .github/workflows/repo-backup.yml`
Expected: both clean.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/repo-backup.yml
git commit -m "fix(repo-backup): declare AWS_ROLE_ARN secret, tighten timeout 15 -> 8"
```

---

### Task 9: `backup.yml` — call `repo-backup.yml` instead of duplicating it

**Files:**
- Modify: `.github/workflows/backup.yml` (full-file rewrite — depends on Task 8 already having landed, since this passes `secrets: { AWS_ROLE_ARN: ... }` to a workflow_call that only accepts a declared secret after Task 8's fix)

- [ ] **Step 1: Replace the whole file**

Replace the entire contents of `.github/workflows/backup.yml` with:
```yaml
name: Backup to S3

on:
  schedule:
    - cron: "0 2 * * 0"  # weekly, Sunday 02:00 UTC
  workflow_dispatch:

jobs:
  backup:
    uses: ./.github/workflows/repo-backup.yml
    with:
      s3-bucket: ${{ vars.BACKUP_S3_BUCKET }}
      environment: production
    secrets:
      AWS_ROLE_ARN: ${{ secrets.AWS_ROLE_ARN }}
```

This preserves the original's exact behavior (`environment: production`, not `repo-backup.yml`'s own default of `backup`) while removing ~70 duplicated lines.

- [ ] **Step 2: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/backup.yml && actionlint .github/workflows/backup.yml`
Expected: both clean.

Run: `python scripts/check_workflow_invariants.py --strict 2>&1 | grep "backup.yml:"`
Expected: no output for this file specifically — `jobs: backup: uses: ./...` is a reusable-workflow call, exempt from WF001/WF002/WF010 (no steps/permissions/timeout of its own to check) and from WF003/WF004 (local `./` refs need no pin). (`--strict` here is inspection only, not a pass/fail gate — see "Before you start".)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/backup.yml
git commit -m "refactor(backup): call repo-backup.yml instead of duplicating it

Removes ~70 lines of copy-pasted logic; behavior is unchanged
(environment: production, same schedule and manual trigger)."
```

---

### Task 10: `access-analyzer-check.yml` — declare the secret, tighten timeout

**Files:**
- Modify: `.github/workflows/access-analyzer-check.yml`

- [ ] **Step 1: Declare `AWS_ROLE_ARN` under `workflow_call`**

Find (end of the `inputs:` block):
```yaml
      environment:
        description: >-
          GitHub environment to deploy into. The environment must have an AWS_ROLE_ARN
          secret set to the IAM role ARN to assume via OIDC.
        type: string
        default: production

permissions:
```
Replace with:
```yaml
      environment:
        description: >-
          GitHub environment to deploy into. The environment must have an AWS_ROLE_ARN
          secret set to the IAM role ARN to assume via OIDC.
        type: string
        default: production
    secrets:
      AWS_ROLE_ARN:
        description: IAM role ARN for OIDC federation
        required: true

permissions:
```

- [ ] **Step 2: Tighten the timeout**

Find:
```yaml
jobs:
  check-no-public-access:
    name: Check No Public Access
    runs-on: ubuntu-latest
    timeout-minutes: 15
    environment: ${{ inputs.environment }}
```
Replace with:
```yaml
jobs:
  check-no-public-access:
    name: Check No Public Access
    runs-on: ubuntu-latest
    timeout-minutes: 8
    environment: ${{ inputs.environment }}
```

- [ ] **Step 3: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/access-analyzer-check.yml && actionlint .github/workflows/access-analyzer-check.yml`
Expected: both clean.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/access-analyzer-check.yml
git commit -m "fix(access-analyzer-check): declare AWS_ROLE_ARN secret, tighten timeout 15 -> 8"
```

---

### Task 11: `validate-bucket-names.yml` and `gitleaks.yml` — timeout only

**Files:**
- Modify: `.github/workflows/validate-bucket-names.yml`
- Modify: `.github/workflows/gitleaks.yml`

Neither file references `secrets.AWS_ROLE_ARN` (no AWS credentials involved in either), so no WF005 fix applies here — timeout only.

- [ ] **Step 1: `validate-bucket-names.yml`**

Find:
```yaml
jobs:
  validate-bucket-names:
    name: S3 Bucket Naming Convention
    runs-on: ubuntu-latest
    timeout-minutes: 10
```
Replace with:
```yaml
jobs:
  validate-bucket-names:
    name: S3 Bucket Naming Convention
    runs-on: ubuntu-latest
    timeout-minutes: 5
```

- [ ] **Step 2: `gitleaks.yml`**

Find:
```yaml
jobs:
  gitleaks:
    name: Secret Scan
    runs-on: ubuntu-latest
    timeout-minutes: 15
```
Replace with:
```yaml
jobs:
  gitleaks:
    name: Secret Scan
    runs-on: ubuntu-latest
    timeout-minutes: 10
```

- [ ] **Step 3: Verify**

Run: `yamllint -c .yamllint.yml .github/workflows/validate-bucket-names.yml .github/workflows/gitleaks.yml && actionlint .github/workflows/validate-bucket-names.yml .github/workflows/gitleaks.yml`
Expected: both clean.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/validate-bucket-names.yml .github/workflows/gitleaks.yml
git commit -m "fix: tighten timeouts on validate-bucket-names (10->5) and gitleaks (15->10)"
```

---

### Task 12: Clean up `workflow-invariants-baseline.yml`

**Files:**
- Modify: `.github/workflow-invariants-baseline.yml`

Tasks 2–10 above resolve 8 of the 12 currently-baselined findings. Remove exactly those 8 lines (plus their comment blocks where the whole block becomes empty) — leave WF004 (`@main` pinning), WF007 (heredoc delimiter), WF011 (intentional unused `smoke-test-url`), and WF012 (README doc drift) untouched; none of those are fixed by this plan.

- [ ] **Step 1: Remove the 4 resolved WF005 entries**

Delete these 4 entries (and their preceding `# --- WF005 ... ---` comment block, since after this task nothing under it remains):
```yaml
  # --- WF005: undeclared secrets force callers into `secrets: inherit` -------
  # Review finding S4. Fix: add a workflow_call.secrets block to each; three
  # other workflows already do it correctly and serve as the pattern.
  - fingerprint: "WF005:.github/workflows/access-analyzer-check.yml:references secrets.AWS_ROLE_ARN but does not declare it"
    review: S4
  - fingerprint: "WF005:.github/workflows/cdk-deploy.yml:references secrets.AWS_ROLE_ARN but does not declare it"
    review: S4
  - fingerprint: "WF005:.github/workflows/repo-backup.yml:references secrets.AWS_ROLE_ARN but does not declare it"
    review: S4
  - fingerprint: "WF005:.github/workflows/static-site-deploy.yml:references secrets.AWS_ROLE_ARN but does not declare it"
    review: S4

```

- [ ] **Step 2: Remove the 4 resolved WF008 entries**

Delete these 4 entries (and their preceding `# --- WF008 ... ---` comment block):
```yaml
  # --- WF008: no concurrency group on PR-check workflows --------------------
  # Review finding E1. Likely the largest runner-minute saving available.
  - fingerprint: "WF008:.github/workflows/cdk-review.yml:job 'review'"
    review: E1
  - fingerprint: "WF008:.github/workflows/python-ci.yml:job 'ci'"
    review: E1
  - fingerprint: "WF008:.github/workflows/self-test.yml:job 'test'"
    review: E1
  - fingerprint: "WF008:.github/workflows/static-site-review.yml:job 'review'"
    review: E1

```

- [ ] **Step 3: Verify the baseline now matches reality exactly**

Run: `python scripts/check_workflow_invariants.py --strict 2>&1 | grep -E "WF005|WF008"`
Expected: no output — both finding classes are genuinely fixed, not just hidden by a stale baseline. (`--strict` bypasses the baseline entirely, so if either class still fired here it would mean the fix in Tasks 2–10 didn't actually take, regardless of what the baseline file says.)

Run: `python scripts/check_workflow_invariants.py --strict --format json | python3 -c "import json,sys,collections; d=json.load(sys.stdin); print(collections.Counter(f['check'] for f in d['blocking'] + d['advisory']))"`
Expected: `WF004` (×17 — the original 12, plus 5 new entries this plan added: every workflow that now calls the new `log-metadata` composite action does so at `@main`, same as its other internal action references), `WF007` (×2, untouched), `WF011` (×2, untouched, intentional), `WF012` (×1, untouched, out of scope). 22 total. **Deviation from the original plan:** each of Tasks 2–6 discovered and fixed this in-flight rather than deferring it to this task — `test_baseline_has_no_stale_entries` (a pytest meta-test over the baseline file) fails immediately if a fix leaves a stale entry, so each task's own gate-green verification required adding the new WF004 entry and removing the newly-stale one at the same time, not batching it here.

Run: `./scripts/local-ci.sh`
Expected: `PASS` — this is the actual gate (baseline-applied), and the one that matters. Do not run `--strict` expecting `PASS` here; per "Before you start," it never passes while WF012 remains un-baselined, which is expected and not a regression.

- [ ] **Step 4: Commit**

```bash
git add .github/workflow-invariants-baseline.yml
git commit -m "chore: remove resolved WF005 and WF008 baseline entries

Both finding classes are now genuinely fixed (secrets declared
explicitly; concurrency groups added), not just baselined."
```

---

### Task 13: Final validation and PR

**Files:** none (validation only)

- [ ] **Step 1: Full gate run**

Run: `./scripts/local-ci.sh`
Expected: `PASS`. Read the full summary, not just the exit code.

- [ ] **Step 2: Full pytest run (not just the workflow-invariants tests)**

Run: `pytest tests/ -v`
Expected: all tests pass (this repo's existing suite for `validate_bucket_names.py`, `check_no_public_access.py`, and `check_workflow_invariants.py` — none of which this plan modifies, so a regression here would indicate an unintended side effect).

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/ci-cd-consistency-efficiency
gh pr create --title "fix: CI/CD workflow consistency and efficiency fixes" --body "$(cat <<'EOF'
## Summary

Implements the shared-workflow half of docs/superpowers/specs/2026-07-26-ci-cd-consistency-and-efficiency-design.md (#139). Caller-repo migrations are a separate follow-up.

- cdk-review.yml / static-site-review.yml: add missing `environment` input (AWS_ROLE_ARN is an environment secret; without this the job silently skipped every check), dedupe synth 3->2 (cdk-review) by isolating the CDK Nag synth to its own output dir, add matrix/ref-aware concurrency, flip enable-ci-logs default to false, tighten timeouts.
- cdk-deploy.yml / static-site-deploy.yml: declare AWS_ROLE_ARN explicitly instead of relying on secrets: inherit, add a pre-deploy synth reused via --app cdk.out, make the smoke test actually fail the job on a bad status (new smoke-test-required input, default true), add a stacks input to static-site-deploy for parity, flip enable-ci-logs default, tighten timeouts.
- python-ci.yml: matrix-aware concurrency group (previously left to callers via a comment, never enforced), enable-ci-logs default false.
- backup.yml: now calls repo-backup.yml instead of duplicating it.
- repo-backup.yml / access-analyzer-check.yml: declare AWS_ROLE_ARN explicitly.
- New actions/log-metadata composite: de-duplicates a block that was copy-pasted in 5 workflows.
- Removes 8 now-resolved findings (4x WF005, 4x WF008) from workflow-invariants-baseline.yml.

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [x] Infrastructure / CI change
- [ ] Documentation

## Checklist

- [x] I have tested these changes locally (or explained why that isn't possible) — ./scripts/local-ci.sh passes; yamllint/actionlint clean on every touched file; WF005/WF008 findings confirmed gone under --strict
- [x] I have updated documentation where relevant — README.md left untouched deliberately (already tracked as separate documentation-drift backlog; these changes only add inputs, never invalidate an existing example)
- [x] CI checks (lint, tests, synth/diff, security scans) pass
- [x] No secrets, credentials, or account IDs are hardcoded

## Related issues

Implements #139. Caller-repo migration (security.yml -> access-analyzer-check.yml, trigger fixes, path filters) is a separate follow-up plan per that spec's rollout order.
EOF
)"
```

- [ ] **Step 4: Report the PR URL**

Confirm the PR was created and report its URL back.

---

## Out of scope (deliberately not touched by this plan)

- **S1** (least-privilege review role, subject-scoped OIDC trust policy, `role-duration-seconds`), **S6** (tag/SHA-pin internal `uses:` references), **S8** (hash-locked CI tool installs), **E5** (split `cdk-review`/`static-site-review` into parallel jobs) — all larger, separate initiatives already captured in `docs/reviews/2026-07-25-actions-security-efficiency-review.md` and partially tracked via the baseline file (WF004 for S6). Not part of the merged spec this plan implements.
- **`--verbose` on `cdk deploy`** (review finding S3, log-leakage risk) — not in the merged spec; flagged here as a follow-up rather than a drive-by change.
- **README.md updates** for the new inputs this plan adds (`environment`, `stacks`, `smoke-test-required`) — `README.md` is already substantially behind the actual workflow interfaces (tracked separately as "Documentation drift" in the 2026-07-25 review and WF012's baseline entry); patching only the inputs this plan touches would leave it in a confusing half-updated state. Full README reconciliation is its own follow-up.
- **Caller-repo migrations** (14 repos onto `access-analyzer-check.yml`, trigger fixes, path filters, per-repo `enable-ci-logs: true` opt-in) — per the spec's own rollout order, this is a second wave that starts only after this PR merges, and needs a fresh caller-repo inventory at that time (5 repos in the original spec's caller list no longer exist, confirmed intentional cleanup mid-spec-writing).
