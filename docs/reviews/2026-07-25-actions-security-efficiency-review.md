# GitHub Actions Deep Review — Security & Efficiency

**Date:** 2026-07-25
**Scope:** `.github/workflows/*.yml` (11 workflows), `.github/actions/*/action.yml` (3 composite actions), `scripts/*.py` (2 helper scripts), `.github/dependabot.yml`
**Baseline:** `yamllint -c .yamllint.yml .github/` clean; `pytest tests/` 48 passed.
**Tooling:** `zizmor 1.28.0 --persona=auditor --offline` → 36 findings (12 high, 11 medium, 13 low). Findings below that zizmor corroborates are marked *(zizmor)*.

This is a recommendations document. Nothing in the workflows was changed.

---

## Executive summary

The estate is in decent shape on the basics that usually go wrong: third-party actions are SHA-pinned, `persist-credentials: false` is set on every checkout, every job has a `timeout-minutes`, caller-controlled shell inputs are passed through `env:` rather than interpolated into `run:`, and `github-script` reads the CDK diff from `process.env` instead of `${{ }}`. Several items already closed in `TODO.md` (script-injection guard on `CDK_STACKS`, the CDK Nag silent-failure fix, the `github-script` diff truncation) hold up under review.

The problems that remain cluster into four themes:

1. **The PR-review workflows hold real AWS credentials while executing PR-authored code.** This is the highest-severity issue in the repo and it is structural, not a typo (S1).
2. **The most important gate can silently do nothing.** `cdk-review.yml` skips synth, diff and Access Analyzer with a `::warning::` when `AWS_ROLE_ARN` resolves empty — which is exactly what happens under the setup the README documents (S2).
3. **CI log shipping is the largest unforced risk-and-cost surface.** `tee` writes files *before* GitHub's secret masking, and those files go to S3/CloudWatch by default on every run (S3, E7).
4. **Wall-clock waste is concentrated in a few fixable places**: no concurrency cancellation, three CDK synths per review, a redundant `pip install`, and an npm cache key that guarantees the cache never updates (E1–E4).

Highest-leverage single change: **add `zizmor` and `actionlint` to `self-test.yml`.** Most of the low/medium findings below are things a linter should be catching on every PR to this repo rather than in a periodic manual review.

---

## Security

### S1 — Review workflows execute untrusted PR code with AWS credentials held *(High)*

**Files:** `cdk-review.yml:151-225`, `static-site-review.yml:158-191`

`cdk-review` assumes the OIDC role (`:151`) and then runs, in order, `cdk synth` (`:159`), the Access Analyzer action (`:171`), `CDK_NAG=true cdk synth` (`:190`), and `cdk diff` (`:210`). `cdk synth` executes `app.py` — arbitrary Python from the PR head. Before that, `Install dependencies` (`:83`) has already run `pip install -r requirements.txt` from the PR head, which executes arbitrary `setup.py`/build-backend code. `static-site-review` additionally runs `npm ci` and `npm run build` (`:86-126`), i.e. arbitrary `postinstall` scripts.

Anyone who can open a PR — plus every transitive dependency in `requirements.txt` and `package-lock.json` — therefore gets code execution in a job that holds credentials for the role behind `AWS_ROLE_ARN`, for the default 1-hour session. If that is the same role the deploy workflows use (which `secrets: inherit` and a single `production` environment make the path of least resistance), a PR comment is a production credential.

This is inherent to running `cdk diff` on a PR — the diff genuinely needs read access to the deployed stacks. The goal is to shrink the blast radius, not eliminate the pattern:

- **Use a distinct review role.** Scope it to `cloudformation:GetTemplate`/`Describe*`/`GetTemplateSummary`, `accessanalyzer:CheckNoPublicAccess`, `sts:GetCallerIdentity`, and `s3:PutObject` on the CI-logs prefix only. Nothing that can mutate infrastructure. Pass it as a separate secret (`AWS_REVIEW_ROLE_ARN`) so `secrets: inherit` can't accidentally hand the deploy role to a PR job.
- **Scope the OIDC trust policy by subject.** `repo:Specter099/<repo>:pull_request` for the review role, `repo:Specter099/<repo>:environment:production` for the deploy role. This is the control that actually prevents a review job from assuming the deploy role even if the ARN leaks.
- **Add `role-duration-seconds: 900`** to both review workflows. A stolen 15-minute credential is materially less useful than a 60-minute one.
- **Split the job (see E5).** Run `pip install` / `npm ci` / lint / tests / bandit in a job with *no* credentials, and do credentialed synth/diff in a second job. This does not fully isolate `cdk synth` (which still runs app code), but it removes dependency-install and test execution — the largest untrusted-code surface — from the credentialed job entirely, and it's a wall-clock win regardless.

### S2 — `cdk-review` reports success while skipping every infrastructure check *(High)*

**Files:** `cdk-review.yml:60-68, 151-269`; same shape in `python-ci.yml:230-233`

The review job declares no `environment:`. `README.md` and `CLAUDE.md` both document `AWS_ROLE_ARN` as an **environment** secret on `production`. Environment secrets are only available to jobs that declare `environment:` — so under the documented configuration, `secrets.AWS_ROLE_ARN` in `cdk-review` resolves to the empty string.

Every AWS-dependent step is gated on `if: env.AWS_ROLE_ARN != ''` (`:153`, `:160`, `:172`, `:191`, `:211`, `:228`). All of them skip. The job then emits `::warning::AWS_ROLE_ARN secret not set — skipping synth/diff` (`:267-269`) and **exits 0**. A required status check goes green having run zero of: synth, diff, CDK Nag, IAM Access Analyzer.

`static-site-review` has the same missing `environment:` but declares the secret `required: true` and calls `configure-aws-credentials` unconditionally (`:158`), so it fails loudly instead. The two workflows disagree, and the silent one is the dangerous one.

Recommendations, in order of preference:

1. Add `environment: ${{ inputs.environment }}` (new input, default `production`) to the review jobs, matching `cdk-deploy`/`access-analyzer-check`. This makes the documented setup actually work.
2. Add an `require-aws: true` input; when true, fail the job rather than warn if the ARN is empty.
3. At minimum, make the fallback path fail: a review workflow that cannot review should not be able to satisfy a branch-protection rule.

Note that `steps.configure-aws-credentials.outcome == 'success'` at `:303` is a good pattern — it correctly distinguishes "secret unset" from "assume-role failed". The problem is only that the overall job still passes.

### S3 — `tee`'d logs bypass secret masking, then ship to S3/CloudWatch *(High)*

**Files:** all five review/deploy workflows; `actions/ship-logs/action.yml`

GitHub's secret redaction happens in the runner as it processes a step's output stream. `tee "$RUNNER_TEMP/ci-logs/*.log"` forks a copy of that stream **before** the runner sees it, so the on-disk file contains the raw, unmasked text. `ship-logs` then tars that directory and uploads it (`ship-logs:71-77`) and/or writes every line to CloudWatch (`:111-181`).

Both `enable-ci-logs` and `ci-log-destination: both` default to on in every workflow. The highest-risk stream is `cdk deploy --verbose` (`cdk-deploy.yml:110`), which is verbose by design and routinely prints account IDs, ARNs, resolved SSM/Secrets Manager references, and CloudFormation parameter values. Anything GitHub would have starred out in the UI is stored in plaintext in S3 and CloudWatch.

Recommendations:

- Flip `enable-ci-logs` to `false` by default. Opt-in, not opt-out.
- Default `ci-log-destination` to `s3` (matching the composite's own default) rather than `both`.
- Drop `--verbose` from `cdk deploy`, or keep it only in the terminal stream and not in the `tee`'d file.
- Require SSE-KMS on the logs bucket, a bucket policy denying `s3:GetObject` outside a named role, `BlockPublicAcls`/`IgnorePublicAcls`, and a short lifecycle expiry (the CloudWatch path already sets 90-day retention at `ship-logs:100-103`; S3 has no equivalent).
- Consider a redaction pass over `$RUNNER_TEMP/ci-logs` before the ship step, even if only for high-entropy strings and `AWS_SECRET_ACCESS_KEY`-shaped values.

### S4 — Five workflows force callers into `secrets: inherit` *(High)*

**Files:** `access-analyzer-check.yml:54`, `cdk-deploy.yml:93`, `repo-backup.yml:83`, `static-site-deploy.yml:110`, `backup.yml:45` (the last is not `workflow_call`, so it's fine)

`access-analyzer-check.yml`, `cdk-deploy.yml`, `repo-backup.yml` and `static-site-deploy.yml` all reference `secrets.AWS_ROLE_ARN` without declaring a `workflow_call.secrets:` block. An undeclared secret is only populated via `secrets: inherit`, which passes **every** secret the caller repository and its environment hold into the reusable workflow. `README.md` duly instructs `secrets: inherit` in all four usage examples.

`cdk-review.yml`, `static-site-review.yml` and `python-ci.yml` already declare theirs correctly — use them as the pattern:

```yaml
    secrets:
      AWS_ROLE_ARN:
        description: IAM role ARN for OIDC federation
        required: true
```

Then change the README examples to `secrets: { AWS_ROLE_ARN: ${{ secrets.AWS_ROLE_ARN }} }`. This is a cheap fix that meaningfully narrows what a compromised or buggy shared workflow can reach.

### S5 — `GITHUB_OUTPUT` heredoc uses a fixed `EOF` delimiter on attacker-influenced content *(Medium)*

**Files:** `cdk-review.yml:216-222`, `static-site-review.yml:182-188`

```bash
diff_output=$(cdk diff 2>&1) || true
{ echo "diff<<EOF"; echo "$diff_output"; echo "EOF"; } >> "$GITHUB_OUTPUT"
```

`cdk diff` output is derived from PR-authored CDK code — stack names, resource logical IDs, descriptions, tag values. A line consisting of exactly `EOF` terminates the heredoc early, and everything after it is parsed as further `GITHUB_OUTPUT` assignments. That lets a PR set arbitrary step outputs for any later step that consumes them.

Fix: use an unguessable delimiter per invocation.

```bash
delim="EOF_$(openssl rand -hex 16)"
{ echo "diff<<$delim"; echo "$diff_output"; echo "$delim"; } >> "$GITHUB_OUTPUT"
```

The PR-comment step is already safe (it reads `process.env.DIFF`, not `${{ }}`) — this is specifically about the output-file write.

### S6 — Internal actions referenced at mutable `@main` *(Medium)* *(zizmor: 12 × `unpinned-uses`)*

**Files:** `cdk-review.yml:79,173,304`; `cdk-deploy.yml:73,197`; `static-site-review.yml:81,266`; `static-site-deploy.yml:78,215`; `python-ci.yml:274`; `access-analyzer-check.yml:63`; plus `validate-bucket-names.yml:37-42` (second checkout at implicit `main`)

Third-party actions are correctly SHA-pinned. Every *internal* reference is not. A single push to `main` in this repository propagates immediately to every caller — including `cdk-deploy` jobs that hold production credentials. That means the effective security boundary for prod deploys across the whole org is write access to this repo's default branch, with no review-and-roll-forward step in between.

This is `TODO.md` P0 ("Especially `Specter099/.github/.github/actions/{setup-cdk,access-analyzer,ship-logs}@main`") and P1 (`validate-bucket-names.yml` ref pin), still open.

Recommendation: cut release tags (`v1`, `v1.2.0`) for this repo, pin internal `uses:` to the tag's SHA with a `# v1.2.0` comment, and add this repo's own path to `dependabot.yml` so bumps are auto-PR'd. Note `validate-bucket-names.yml:37-42` needs an explicit `ref:` on the checkout, not just a `uses:` pin.

### S7 — `pull-requests: write` review jobs are one caller mistake from a serious escalation *(Medium)*

**Files:** `cdk-review.yml:63-66`, `static-site-review.yml:67-70`, `validate-bucket-names.yml:27-29`

Reusable workflows inherit the caller's trigger. Today callers use `pull_request` (per the documented convention), where fork PRs get a read-only token — so the write permission is unusable from a fork and the design holds. But the workflows themselves place no constraint on the trigger. A caller that switches to `pull_request_target` to "make comments work on forks" would get: checkout of fork-controlled code, a write-scoped `GITHUB_TOKEN`, **and** AWS credentials, in one job.

Recommendations:

- Add a guard step at the top of each review workflow: fail if `github.event_name == 'pull_request_target'`.
- Document the constraint in `CLAUDE.md` next to the existing trigger convention, which currently covers `pull_request` vs `push` but says nothing about `pull_request_target`.
- Consider dropping `pull-requests: write` from `validate-bucket-names.yml` — its comment step (`:60-89`) posts a static template containing no actual violation data, so the log output already tells the developer everything the comment does.

### S8 — Unpinned package installs at runtime *(Medium)* *(zizmor: `adhoc-packages`)*

**Files:** `cdk-review.yml:183`; `static-site-review.yml:151`; `python-ci.yml:176,189,217`; `actions/access-analyzer/action.yml:29`; `actions/setup-cdk/action.yml:49`

`pip install bandit`, `pip install pip-audit`, `pip install pytest-cov`, `pip install --quiet --upgrade boto3 cfn-flip`, `npm install -g "aws-cdk@$CDK_VERSION"` — all resolve to whatever the registry serves at that moment, with no version constraint and no hash pinning. Any of these is a supply-chain foothold into a job that holds AWS credentials (see S1), and it also means CI results aren't reproducible: a bad `bandit` release breaks every caller's PR check simultaneously.

Recommendation: move these into a pinned, hash-locked requirements file shipped with this repo (`.github/requirements-ci.txt` with `--require-hashes`), installed once. `aws-cdk` is at least version-pinned via the `cdk-version` input; the hardcoded `2.1106.1` fallback in `setup-cdk:27` is fine but should be kept in step with what Dependabot sees.

### S9 — `fail-on-public-access: false` is not actually warn-only *(Low)*

**File:** `actions/access-analyzer/action.yml:56-68`, `scripts/check_no_public_access.py:234-319`

The composite maps `fail-on-public-access: false` to `--no-fail-on-public-access`, which only suppresses the exit-1 path for detected violations (`check_no_public_access.py:307-309`). Two other failure paths ignore the flag entirely:

- missing `--template-dir` → `return 1` (`:236-239`)
- any unparseable template or Access Analyzer API error → `return 2` (`:311-316`)

The composite then does `exit "$rc"` (`:68`), so a "warn-only" configuration still hard-fails on an incomplete scan. That's defensible as a security default — an incomplete scan *shouldn't* silently pass — but it contradicts the input's documented meaning ("set 'false' for warn-only"). Either rename the input to `fail-on-violation` and document the exit-2 behaviour, or add a separate `fail-on-incomplete-scan` input.

Related, same file: the `Notify on public access detected` step (`:70-73`) is gated on `if: failure()`, which in a composite reflects job-level failure. It therefore prints `::error::IAM Access Analyzer detected resources that grant public access` when the *`pip install`* failed, or when `cfn-flip` failed, or on an exit-2 incomplete scan — none of which are public-access findings. Gate it on the actual check step's outcome instead.

### S10 — Access Analyzer coverage gaps *(Low)*

**File:** `scripts/check_no_public_access.py:95-108, 33-91`

Three separate gaps worth knowing about, all reasonable trade-offs but none currently documented for callers:

1. `POLICY_MAP` covers 6 resource types. Access Analyzer `CheckNoPublicAccess` also supports (among others) `AWS::Lambda::Function` permissions, `AWS::EFS::FileSystem`, `AWS::Backup::BackupVault`, `AWS::ApiGateway::RestApi`, `AWS::OpenSearchService::Domain`, and `AWS::IAM::ManagedPolicy`. A publicly-invokable Lambda or a public API Gateway resource policy passes this check today.
2. Only *explicit* policy resources are examined. An `AWS::S3::Bucket` with a permissive ACL or without `PublicAccessBlockConfiguration`, and no separate `AWS::S3::BucketPolicy` resource, is never checked. Worth pairing with a Checkov/cfn-nag rule rather than extending this script.
3. `resolve_intrinsics` (`:33-91`) handles `Ref`, `Fn::Join`, `Fn::Sub`, `Fn::GetAtt`. `Fn::If`, `Fn::Select`, `Fn::ImportValue` and `Fn::FindInMap` fall through as dicts → invalid policy document → API error → exit 2 (a hard fail, per S9). And a `Principal` of `{"Ref": "SomeParameter"}` becomes a placeholder ARN, so a template whose parameter *defaults* to `*` is a false negative. The docstring acknowledges the substitution; it should also state the false-negative consequence.

### S11 — `check_no_public_access.py` crashes instead of exiting 2 without credentials *(Low)*

**File:** `scripts/check_no_public_access.py:251-254`

`except ClientError` doesn't cover `NoCredentialsError` (a `BotoCoreError`). Verified locally with credentials stripped: the script raises an unhandled `botocore.exceptions.NoCredentialsError` traceback rather than the documented exit code. Same exposure on the `check_no_public_access` call at `:160`, where a `NoCredentialsError` or `EndpointConnectionError` escapes the `except ClientError` at `:172`. Widen to `except (ClientError, BotoCoreError)` and return 2.

### S12 — No `role-session-name` anywhere *(Low)*

**Files:** all 6 `configure-aws-credentials` call sites

Every assume-role uses the action's default session name, so CloudTrail cannot attribute an API call to a repository, workflow, or run. Add `role-session-name: gha-${{ github.event.repository.name }}-${{ github.run_id }}` (64-char limit, so prefer repo name over full `github.repository`). This costs nothing and is the difference between a five-minute and a five-hour incident investigation.

### S13 — `gitleaks.yml` has no way to supply a licence *(Low)*

**File:** `gitleaks.yml:3-5, 20-23`

`gitleaks/gitleaks-action@v3` requires `GITLEAKS_LICENSE` for organization-owned repositories above the free tier. The workflow declares no `workflow_call.secrets:` block at all, so a caller cannot pass one without switching to `secrets: inherit`. Add an optional `GITLEAKS_LICENSE` secret now; the fallback documented in `TODO.md` P2 (`docker://zricethezav/gitleaks` — pin it by digest, not `:latest`) remains the escape hatch. `python-ci.yml:165-168` has the same gap.

### S14 — `backup.yml` hygiene *(Low)*

**File:** `backup.yml:4-6, 13`

- The scheduled job has no `if: github.repository == 'Specter099/.github'` guard, so any fork that keeps Actions enabled runs the weekly backup and fails against the upstream role.
- `environment: production` on a backup job means backups are subject to production approval and branch-protection rules. `TODO.md` P1 already proposes `environment: backup`; that's the right call.
- No `concurrency` group *(zizmor)*, so a `workflow_dispatch` during the scheduled window races the scheduled run for the same S3 key.

---

## Efficiency

### E1 — No concurrency cancellation on any review workflow *(High)* *(zizmor: `concurrency-limits`)*

**Files:** `cdk-review.yml`, `static-site-review.yml`, `python-ci.yml`, `validate-bucket-names.yml`, `access-analyzer-check.yml`, `self-test.yml`, `backup.yml`, `repo-backup.yml`

Only the two deploy workflows set `concurrency` (correctly, with `cancel-in-progress: false`). Every review workflow lacks it, so three pushes to a PR in five minutes run three complete pipelines to completion — including three sets of `cdk synth`, three Access Analyzer sweeps, and three log shipments.

`python-ci.yml:6-9` documents this as the caller's responsibility. That's the wrong default: it means every caller repo has to remember, and the ones that forget pay silently. Set it job-level inside the reusable workflow:

```yaml
    concurrency:
      group: cdk-review-${{ github.workflow }}-${{ github.ref }}
      cancel-in-progress: true
```

For `python-ci`, the group **must** include `${{ matrix.python-version }}` or the matrix legs cancel each other.

This is likely the single largest runner-minute saving available, and it's four lines per file.

### E2 — `cdk-review` synthesizes the CDK app three times *(High)*

**File:** `cdk-review.yml:159-225`

1. `CDK Synth` (`:166`) — produces `cdk.out`
2. `CDK Nag` (`:199`) — `CDK_NAG=true cdk synth`, a second full synth
3. `CDK Diff` (`:216`) — `cdk diff` re-synthesizes internally, a third

Synth is typically the slowest step in a CDK PR check (tens of seconds to minutes on a real app). Two of the three are avoidable:

- `cdk diff --app cdk.out` reuses the assembly from step 1 instead of re-synthesizing. This also makes the diff consistent with the templates Access Analyzer actually scanned — today they are two independent synths that could in principle differ.
- The Nag synth genuinely needs a different environment variable, so it can't share the first assembly. But it's independent of everything else in the job, so it belongs in a **parallel job** (see E5) rather than serially in the critical path. If parallelising isn't wanted, running Nag unconditionally in the first synth and filtering the captured output achieves the same in one pass.

### E3 — Redundant `pip install -r requirements.txt` *(High)*

**File:** `cdk-review.yml:83-88`

`setup-cdk` (`:51-55`) already installs `requirements.txt` — that's its default `requirements-path`. The immediately following `Install dependencies` step installs it again. `TODO.md` P1 estimates ~15–30s per run; on a CDK project with `aws-cdk-lib` and boto3 it is often more.

Keep the `requirements-dev.txt` half of that step (`:86-88`) — `setup-cdk` doesn't install it — and drop the `requirements.txt` line.

### E4 — npm cache key guarantees the cache never updates *(High)*

**File:** `actions/setup-cdk/action.yml:39-43`

```yaml
    - name: Cache npm
      uses: actions/cache@...
      with:
        path: ~/.npm
        key: npm-${{ runner.os }}-cdk-${{ steps.cdk-ver.outputs.version }}
```

The key contains no content hash, so it is constant for a given CDK version. `actions/cache` skips its post-run save on an exact key hit — so the cache is populated on the first run for a CDK version and then **frozen forever**. Two consequences:

- For `static-site-review`/`static-site-deploy`, the caller's frontend dependencies are cached only as they existed on that first run. Every `package-lock.json` change after that re-downloads from the registry on every run, permanently. This is `TODO.md` P2, and it's underrated there — it's a per-run cost on the two heaviest workflows.
- The cache also can't be invalidated when it goes stale or bad, short of bumping the CDK version.

Recommendation — do both:

```yaml
        key: npm-${{ runner.os }}-cdk-${{ steps.cdk-ver.outputs.version }}-${{ hashFiles('**/package-lock.json') }}
        restore-keys: |
          npm-${{ runner.os }}-cdk-${{ steps.cdk-ver.outputs.version }}-
```

and pass `cache: npm` + `cache-dependency-path` to the `actions/setup-node` call at `:35-37`, which handles this correctly out of the box. Note `setup-python`'s `cache: pip` (`:33`) has the same shape of issue less severely — it defaults to hashing `**/requirements.txt`, which works, but an explicit `cache-dependency-path` tied to `inputs.requirements-path` would be more predictable for the monorepo callers.

### E5 — Everything runs in one serial job *(Medium)*

**File:** `cdk-review.yml` (all 18 steps in one job); same in `static-site-review.yml`

Lint, unit tests, pip-audit and bandit have no dependency on AWS credentials or on each other, yet they run serially ahead of (and in bandit's case, `:178`, *after*) the credentialed steps. `TODO.md` P2 proposes reordering for faster failure; splitting into parallel jobs is strictly better and solves S1's isolation problem at the same time:

- **job `static-checks`** (no `id-token`, no credentials): checkout → setup → install → ruff → bandit → pytest → pip-audit
- **job `infra`** (`id-token: write`): checkout → setup → configure-aws → synth → access-analyzer → diff → comment
- **job `nag`** (`id-token: write`): checkout → setup → configure-aws → nag synth

Wall clock becomes `max(...)` instead of `sum(...)`. The cost is repeated checkout+setup per job (~20–30s each, largely cache-served), which the parallelism more than repays on any non-trivial app. Log shipping then needs a small `needs: [...]` collector job with `if: always()`.

Also at `:91` and `:110`: `if: hashFiles('requirements-dev.txt') != '' && success()` — the `&& success()` is redundant, that's the default step condition.

### E6 — QEMU and Buildx set up unconditionally *(Medium)*

**File:** `static-site-deploy.yml:113-119`

`docker/setup-qemu-action` with `platforms: arm64` plus `setup-buildx-action` costs roughly 30–60s per deploy. They're only needed when the CDK app bundles Lambda functions (or containers) for arm64 via Docker. A pure static-site deploy — S3 + CloudFront, which is what the workflow's name promises — needs neither. Gate both behind an `enable-docker-bundling` input, default `false`.

### E7 — CloudWatch shipping is per-line, and `both` is the default *(Medium)*

**File:** `actions/ship-logs/action.yml:151-181`; `ci-log-destination` defaults in all five workflows

Every line of every log file becomes an individual CloudWatch log event (`:161-169`), each carrying 26 bytes of accounting overhead on top of the message. For `cdk deploy --verbose` output this is a lot of events and a lot of ingest ($0.50/GB) — and because `ci-log-destination` defaults to `both` in all five workflows (overriding the composite's own `s3` default at `:13`), every run pays for S3 storage *and* CloudWatch ingest *and* 90 days of CloudWatch storage for the same bytes.

Recommendations:
- Default `ci-log-destination` to `s3`. S3 is the cheap, durable copy; CloudWatch should be opt-in for the runs someone actually wants to query.
- If CloudWatch stays, batch lines into fewer, larger events rather than one per line. The existing batching (`:165`) batches the *API calls*, not the events.
- Delete the dead `sequence_token` plumbing (`:124-147`) — `put-log-events` has ignored `--sequence-token` since August 2023, and removing it cuts the Python block substantially. This is `TODO.md` P1.
- Failures are swallowed: `create-log-group`/`put-retention-policy`/`create-log-stream` are all `2>/dev/null || true` (`:97-108`), and `put_events` only prints a warning on failure (`:149`). A permissions misconfiguration produces a green "logs shipped" step and zero logs. At minimum, count failed batches and fail the step if any failed.

### E8 — `fetch-depth: 0` in the backup workflows is pure waste *(Medium)*

**Files:** `backup.yml:18-21`, `repo-backup.yml:51-54`

Both fetch the complete history, then `git archive --format=zip HEAD` (`backup.yml:38`, `repo-backup.yml:76`) writes a snapshot of the working tree at HEAD. The history is fetched and immediately discarded.

Two ways to resolve, depending on intent:

- If a snapshot is what's wanted: drop `fetch-depth: 0`. On a large repo this is the majority of the job's runtime.
- If a *backup* is what's wanted: the current artifact contains no history, no branches, no tags — restoring from it loses everything except the tip of the default branch. `git clone --mirror` + `git bundle create backup.bundle --all` produces a genuinely restorable backup and justifies the full fetch. Worth an explicit decision, since the workflow is named "Backup".

Minor, same files: `du -sh "$FILENAME"` (`:39` / `:77`) reports disk usage (block-rounded), not archive size — `stat -c %s` or `du -b` is what the summary means.

### E9 — Full second checkout to fetch two scripts *(Medium)*

**File:** `validate-bucket-names.yml:37-42`

The second `actions/checkout` clones all of `Specter099/.github` to use `scripts/validate_bucket_names.py`. Add `sparse-checkout: scripts/` and `sparse-checkout-cone-mode: false`, plus the `ref:` pin from S6. Small, but this workflow is meant to be a fast PR gate and the checkout is a meaningful share of its runtime.

### E10 — Frontend built twice across review and deploy *(Medium)*

**Files:** `static-site-review.yml:116-126`, `static-site-deploy.yml:95-105`

`TODO.md` P3 already frames the decision (CI uploads `dist/` vs. CD rebuilds). Worth resolving rather than leaving open: the current state pays for two builds *and* leaves a drift window where the artifact CI validated is not the artifact CD ships. Given the deploy workflow's `concurrency` already serialises deploys, Option A (upload in review, download in deploy) is the stronger choice — it makes the deployed bundle byte-identical to the reviewed one, which is the whole point of the review gate.

### E11 — `ENABLE_LOGS` boilerplate repeated ~30 times *(Low)*

**Files:** all five review/deploy workflows

Every logged step carries the same `if [ "$ENABLE_LOGS" = "true" ]; then <cmd> 2>&1 | tee <file>; else <cmd>; fi` block, with the command duplicated in both branches — a maintenance hazard, since the two copies can drift (and the `set -o pipefail` placement differs subtly between files).

`TODO.md` P2 proposes a `run-with-optional-log` composite action. A simpler option: always `tee` into `$RUNNER_TEMP/ci-logs` (creating the directory unconditionally) and let the ship step be the only thing gated on `enable-ci-logs`. Writing a file to the runner's own temp costs nothing, and the branch disappears entirely.

### E12 — Matrix jobs overwrite each other's shipped logs *(Low)*

**Files:** `python-ci.yml:103-106, 272-280`; `actions/ship-logs/action.yml:50-56`

The S3 key is `${S3_PREFIX}/${REPO}/${SAFE_WORKFLOW}/${RUN_ID}-${ATTEMPT}` and the CloudWatch stream is `${REPO}/${SAFE_WORKFLOW}/${RUN_ID}-${ATTEMPT}` — neither includes a job or matrix discriminator. With `python-versions: '["3.11","3.12"]'`, both legs upload to the identical `logs.tar.gz` key (last writer wins, silently) and interleave into a single CloudWatch stream. Add an optional `log-name-suffix` input to `ship-logs` and pass `${{ matrix.python-version }}`.

### E13 — Assorted correctness nits *(Low)*

- **PR comment duplicates on busy PRs.** `cdk-review.yml:243-247` / `static-site-review.yml:209-213` call `issues.listComments` with no pagination. The REST default is `per_page=30`, so once a PR has more than 30 comments the existing CDK-diff comment isn't found and a new one is created on every run. Use `github.paginate(github.rest.issues.listComments, {...})`, or at minimum `per_page: 100` with `sort: 'created', direction: 'desc'`.
- **Unawaited API call.** `validate-bucket-names.yml:84-89` calls `github.rest.issues.createComment({...})` without `await`. `github-script` resolves the script's return value; a floating promise can be dropped when the action exits. Add `await`.
- **`cdk diff` failures are indistinguishable from an empty diff.** `cdk-review.yml:216` / `static-site-review.yml:182`: `diff_output=$(cdk diff 2>&1) || true` discards the exit code, so a credentials error or a stack-lookup failure posts its error text as "the diff" and the step passes. Capture `rc` and emit at least a `::warning::` on non-zero.
- **`pip install --upgrade pip` as a dedicated step** (`python-ci.yml:124-125`) costs a step's overhead for little benefit on a `setup-python` runner; fold it into the install step.
- **`pip-audit` invoked inconsistently.** `cdk-review.yml:141` audits `-r requirements.txt`; `python-ci.yml:178` audits the whole installed environment. The latter is the more useful check (it sees transitive pins); pick one.
- **Bandit exclude paths differ** between `cdk-review.yml:185` (`./.venv,./cdk.out,./lambda_layer`) and `python-ci.yml:191` (`.venv,cdk.out`). Bandit's `--exclude` matching is path-prefix sensitive; the leading `./` forms are the reliable ones.

### E14 — Dependabot configuration *(Low)* *(zizmor: `dependabot-cooldown`)*

**File:** `.github/dependabot.yml`

- No `cooldown`. A compromised action release gets an auto-PR within a day, and the whole point of SHA pinning is to buy time for a bad release to be caught. Add `cooldown: { default-days: 7 }`.
- All actions are grouped into a single PR (`:12-15`). One bump that breaks CI blocks every other bump behind it. Consider splitting security updates from version updates, or at least separating the AWS actions from the rest.
- The `directories` list doesn't include this repo's own reusable-workflow references once S6's internal pinning lands — add whatever paths the pinned internal `uses:` end up in.

### E15 — `validate_bucket_names.py` accuracy *(Low)*

**File:** `scripts/validate_bucket_names.py:51`

`BUCKET_NAME_RE = r"^[a-z0-9][a-z0-9-]+-\d{12}-[a-z]{2}-[a-z]+-\d-an$"` — verified locally:

| Name | Result | Should be |
|---|---|---|
| `logs-123456789012-us-east-1-an` | pass | pass |
| `logs-123456789012-ap-southeast-2-an` | pass | pass |
| `logs-123456789012-cn-north-1-an` | pass | pass |
| `logs-123456789012-us-gov-west-1-an` | **fail** | pass |
| 106-char name, otherwise valid | **pass** | fail (S3 limit is 63) |

The region group assumes exactly three segments, so four-segment regions (`us-gov-west-1`, `us-gov-east-1`) are reported as violations. And there's no length check, so a name CloudFormation will reject at deploy time passes the gate that exists to catch exactly that. Suggested: `^(?=.{3,63}$)[a-z0-9][a-z0-9-]+-\d{12}-[a-z]{2}(-[a-z]+)+-\d-an$`.

Separately, `scan_directory` (`:130-136`) walks every `.py` under the scan root, excluding only `cdk.out`, `.venv`, `venv`, `node_modules`, `__pycache__`. Vendored dependencies, `build/`, `dist/`, and `site-packages` under a differently-named virtualenv all get scanned, producing violations for code the caller doesn't own. Add an `--exclude` argument.

---

## Documentation drift

These aren't security or performance issues, but two of them will actively break a caller who follows the README.

1. **`README.md` documents a `python-version` input for `python-ci`; the actual input is `python-versions`** and takes a JSON array (`python-ci.yml:14-19`). GitHub rejects unexpected inputs to a reusable workflow, so the README's `python-ci` example fails outright.
2. **`CLAUDE.md` claims `cdk-review` runs Checkov.** It does not — there is no Checkov step. Either add `checkov -d .` (a good fit for the S10 gaps) or drop the claim. `TODO.md` P2.
3. **The README documents 3 inputs per workflow; the workflows have 8–12.** Missing entirely: every `enable-ci-logs` / `ci-log-*` input, `stacks` and `environment` (`cdk-deploy`), `enable-access-analyzer` (`cdk-review`), and `coverage` / `coverage-threshold` / `bandit` / `pip-audit` / `install-command` / `tests-dir` (`python-ci`). `access-analyzer-check`, `validate-bucket-names` and `gitleaks` aren't documented at all.
4. **The README's "Required secret" note** says all workflows assume `AWS_ROLE_ARN`; `cdk-review` declares it optional and `python-ci` needs it only for log shipping. Worth stating which workflows hard-require it — this is the documentation half of S2.
5. **`cdk-review.yml` still accepts an unused `smoke-test-url`** (`:18-23`). It carries a comment explaining why, which satisfies `TODO.md` P1; leaving it is fine.
6. **YAML header style is inconsistent** — `cdk-review.yml` and `python-ci.yml` open with `---` + `"on":`, the other nine don't. `TODO.md` P2. Cosmetic, but pick one and let yamllint enforce it.

---

## Suggested sequencing

**First — make the gates real and stop the credential exposure**
1. S2 — `environment:` on the review jobs, and fail rather than warn when the ARN is missing. Without this, some of the checks below are protecting nothing.
2. S1 — separate least-privilege review role, subject-scoped OIDC trust policy, `role-duration-seconds: 900`.
3. S3 — `enable-ci-logs` default `false`, `ci-log-destination` default `s3`, drop `--verbose` from the `tee`'d deploy stream.
4. S4 — declare `workflow_call.secrets:` in the four workflows missing it; update README examples off `secrets: inherit`.

**Second — cheap, high-return**
5. E1 — job-level `concurrency` + `cancel-in-progress` in every review workflow (matrix-aware for `python-ci`).
6. E3 — delete the duplicate `pip install`.
7. E2 — `cdk diff --app cdk.out`.
8. E4 — fix the npm cache key; use `setup-node`'s built-in cache.
9. S5 — random `GITHUB_OUTPUT` heredoc delimiter.
10. **Add `zizmor --persona=auditor` and `actionlint` to `self-test.yml`.** This is what keeps S6, E1, E14 and most of the `permissions` findings from coming back. Expect to start with an ignore-file and burn it down.

**Third — structural**
11. S6 — tag this repo, pin internal `uses:` by SHA, add `ref:` to the `validate-bucket-names` checkout.
12. E5 — split `cdk-review` into parallel jobs (also completes S1's isolation).
13. S8 — hash-pinned CI tool requirements file.
14. E10 — decide the CI-artifact-vs-CD-rebuild question and document it.

**Then** the remaining Low items, and reconcile the docs.

---

## Appendix — zizmor summary

`zizmor 1.28.0 --persona=auditor --offline .github/` → 36 findings: 12 high, 11 medium, 13 low, 0 informational.

| Audit | Count | Covered by |
|---|---|---|
| `unpinned-uses` | 12 | S6 |
| `excessive-permissions` | 11 | S7; plus missing top-level `permissions:` blocks on `backup`, `gitleaks`, `self-test`, `repo-backup`, `validate-bucket-names`, both deploys and both reviews |
| `undocumented-permissions` | 8 | — (comment each `id-token: write` / `pull-requests: write`; `python-ci.yml:93-100` already does this well) |
| `concurrency-limits` | 4 | E1 |
| `adhoc-packages` | 1 | S8 |
| `dependabot-cooldown` | 1 | E14 (has an auto-fix) |

The `excessive-permissions` warnings are worth a pass in their own right: eight workflows set `permissions` only at job level, leaving the workflow default in force. Adding `permissions: {}` at the top of each file and granting per-job is the pattern `access-analyzer-check.yml` half-implements (it sets workflow-level `id-token: write`, which zizmor flags as too broad at that scope — it should move to the job).
