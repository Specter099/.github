# Security, Operations & Simplicity Review — 2026-09-25

Scope: every workflow, composite action, and script at `3d42451`. This follows
up on [the 2026-07-25 review](2026-07-25-actions-security-efficiency-review.md)
(S-/E- IDs below refer to it). New findings are tagged **N**.

Findings are ordered by priority. Each one was checked against the code, and
where possible run.

## P0 — fix now

### N1 — The deploy smoke test passes when the site is unreachable *(High, verified)*

**Files:** `cdk-deploy.yml:170-175`, `static-site-deploy.yml:219-226`

```bash
status=$(curl -o /dev/null -s -w "%{http_code}" --max-time 15 "$URL" || echo "000")
if [[ "$status" == "000" || "$status" -ge "400" ]]; then  # fail
```

When curl cannot connect (DNS failure, timeout, connection refused), it still
prints `000` through `-w` and then exits non-zero, so `|| echo "000"` appends a
second `000`. `status` is `000000`. That string is not `"000"`, and `000000 -ge 400`
is false, so the step prints "Smoke test passed — endpoint is reachable."
Reproduced locally against a closed port:

```
status=[000000]
PASSED-WRONGLY
```

This is the failure the smoke test exists to catch, and `smoke-test-required: true`
(the default) does not help. Fix: drop the `|| echo "000"` (the `-w` output
already covers it), or use `status=$(curl ... ) || true`.

### S1 (still open) — PR code runs with the production environment and role *(High)*

**Files:** `cdk-review.yml:73-79`, `static-site-review.yml:85-89`

The S2 fix added `environment: ${{ inputs.environment }}` (default
`production`) to both review jobs. That made synth and diff actually run, but
the review job now takes the **production** environment's `AWS_ROLE_ARN`, which
is the same role `cdk-deploy` uses with `--require-approval never`. The job also
holds `id-token: write` from its first step. So `pip install -r requirements.txt`,
`npm ci` postinstall scripts, `pytest`, and `app.py` can all mint an OIDC token
and assume that role, before or after the `configure-aws-credentials` step.

For this to work at all, the production environment has no branch restriction
and no required reviewers. That means any branch can reach the deploy role.

Fix, in order:
1. Create a `review` environment and a read-only review role (CloudFormation
   `Describe*`/`GetTemplate`, `accessanalyzer:CheckNoPublicAccess`,
   `sts:GetCallerIdentity`). Trust it on `repo:Specter099/<repo>:pull_request`.
2. Default the review workflows' `environment` input to `review`.
3. Restrict the `production` environment to `main`, and trust the deploy role
   only on `repo:Specter099/<repo>:environment:production`.

## P1 — security & correctness

### S6 (still open) — Internal actions at mutable `@main`
17 WF004 baseline entries. A push to `main` here takes effect at once in every
caller's deploy job. Tag, pin by SHA, and let Dependabot bump the pins.

### S5 (still open, trivial) — Fixed `EOF` heredoc delimiter on `cdk diff` output
**Files:** `cdk-review.yml:237-241`, `static-site-review.yml:206-210`. This is a
two-line fix per file (`delim="EOF_$(openssl rand -hex 16)"`) and clears two
baseline entries. It's the cheapest open security item.

### N2 — `cdk-review` goes green without running any AWS checks *(Medium)*
**File:** `cdk-review.yml:56-61, 286-288`

`AWS_ROLE_ARN` is `required: false`. When it's unset, synth, Access Analyzer,
CDK Nag, and diff are all skipped, the job logs a `::warning::`, and it exits 0,
which satisfies a required status check. `static-site-review` declares the same
secret `required: true` and fails loudly, so the two workflows disagree. This is
what's left of S2. Fix: add a `require-aws` input that defaults to `true` and
fail the job when the ARN is missing.

### N3 — Security checks in `cdk-review` skip silently when the tool is absent *(Medium)*
**File:** `cdk-review.yml:104-175`

Lint, tests and `pip-audit` run only if the caller's `requirements-dev.txt`
happens to install `ruff`, `pytest` or `pip-audit`. Otherwise each prints a
`::notice::` and passes. For `pip-audit` that means the dependency audit the
README promises doesn't run for most callers. `static-site-review` installs
`pip-audit` itself, so the two are inconsistent here too. Fix: install
`pip-audit` in the workflow, as `static-site-review` and `bandit` already do.

### N4 — A CloudFormation YAML template that fails conversion is silently not scanned *(Medium)*
**File:** `actions/access-analyzer/action.yml:37-40`

`find ... | while read f; do cfn-flip "$f" "$out" && echo ...; done`: when
`cfn-flip` fails on one template, the `&&` list doesn't trip `-e` and the loop
continues. The step fails only if the *last* file failed. The scan then passes
on the templates that did convert. Fix: `cfn-flip "$f" "$out" || exit 1` inside
the loop, or collect failures and exit non-zero.

### S11 (still open) — `check_no_public_access.py` tracebacks without credentials
`except ClientError` doesn't catch `NoCredentialsError` or
`EndpointConnectionError` (`BotoCoreError`). The documented exit 2 turns into an
uncaught exception.

### S7 / S8 / S12 / S13 (still open)
- **S7:** no guard against `pull_request_target` callers. `validate-bucket-names`
  still asks for `pull-requests: write` just to post a fixed comment.
- **S8:** `pip install bandit`, `pip-audit`, `pytest-cov`, `boto3 cfn-flip` are
  all unpinned, in credentialed jobs.
- **S12:** no `role-session-name` at any of the six assume-role sites.
- **S13:** no way to pass `GITLEAKS_LICENSE`.

## P2 — operations

### N5 — `access-analyzer-check.yml` pip cache can fail the job for YAML-only callers *(Medium)*
**File:** `access-analyzer-check.yml:66-70`

`setup-python` with `cache: pip` errors out when the caller has no
`requirements.txt` or `pyproject.toml`. The `yaml-template-dir` input exists for
exactly those callers ("repos with no CDK app"). What the job installs is
`boto3 cfn-flip` from the composite, which isn't keyed by the caller's files
anyway. Fix: drop `cache: pip`.

### N6 — CDK diff comment isn't updated in place on busy PRs *(Low)*
**Files:** `cdk-review.yml:262-269`, `static-site-review.yml:231-238`

`listComments` isn't paginated (the default is 30 per page), so once a PR has
more than 30 comments, each run posts a new diff comment. Use
`github.paginate(github.rest.issues.listComments, …)`.

### N7 — The bucket-name comment misleads and repeats *(Low)*
**File:** `validate-bucket-names.yml:70-99`

It posts a new comment on every failing push (no update in place). It also
tells callers to run `python scripts/validate_bucket_names.py`, a script that
exists only in this repo. The job log already has the details, so drop the
comment and the `pull-requests: write` permission (S7).

### N8 — Log-shipping failures are invisible *(Low)*
**File:** `actions/ship-logs/action.yml`

`create-log-group`, `put-retention-policy` and `create-log-stream` all run with
`2>/dev/null || true`, and a failed `put-log-events` only prints to stderr. So an
IAM misconfiguration shows up as a green step with nothing shipped. Emit
`::warning::` on failure. S3 (unmasked secrets in tee'd logs) and E12 (matrix
legs overwriting each other's S3 key) are also still open. `cdk deploy --verbose`
is still tee'd in `cdk-deploy.yml`.

### N9 — `python-ci` runs gitleaks once per matrix leg *(Low)*
With `python-versions: '["3.11","3.12"]'`, the full-history secret scan runs
twice. Move it to its own job, or gate it to the first matrix entry.

### N10 — `self-test` runs `go install actionlint` on every run *(Low)*
`actionlint` isn't in `requirements-dev.txt`, so `local-ci.sh` compiles it from
source on every CI run (about 10 s, plus a proxy.golang.org dependency). Cache
`~/go/bin` or download the release binary. Also, `-shellcheck=` turns off shell
linting of `run:` blocks. ShellCheck would likely have flagged N1-class quoting
and `||` mistakes.

### S14 (partially open) — `backup.yml` still runs under `production`
It now calls `repo-backup.yml` and has concurrency, but it still passes
`environment: production` and has no `github.repository ==` fork guard.

## P3 — simplicity

- **E11:** The `ENABLE_LOGS` if/tee/else block is still repeated about 25 times.
  It's the biggest source of workflow length.
- **Role ARN source is inconsistent:** only `repo-backup.yml` accepts
  `vars.AWS_ROLE_ARN`. Pick one convention.
- **`cdk-deploy` vs `static-site-deploy` drift:** only one uses `--verbose` and
  awk timestamps. The smoke-test blocks are copies of each other (N1 exists in
  both).
- **Docs:** `docs/plans/` and `docs/superpowers/` hold about 2,100 lines of
  executed implementation plans. They're history, not guidance, so consider
  archiving them.

## Dead code and drift removed in this change

| What | Where | Why it was dead |
|---|---|---|
| CloudWatch `sequence_token` tracking | `actions/ship-logs/action.yml` | `PutLogEvents` has ignored sequence tokens since 2023 (TODO P1) |
| `SKIP_FILES = {"manifest.json","tree.json","cdk.out"}` | `scripts/check_no_public_access.py` | `rglob("*.template.json")` can never yield those names |
| `skip = {"manifest.json","tree.json"}` | `scripts/validate_bucket_names.py` | Same |
| Unused second return value of `load_baseline` | `scripts/check_workflow_invariants.py` | Every caller discarded it |
| 11 completed items | `TODO.md` | Already shipped (timeouts, CDK Nag fix, stacks guard, npm cache key, reorder, …) or stale |
| `python-version` README example + its WF012 baseline entry | `README.md`, baseline | The input is `python-versions` (a JSON array). Copying the example failed. |
| `repo-backup` default documented as `production` | `README.md` | The actual default is `backup` |
| "checkov" claim; `BACKUP_S3_BUCKET` listed as an environment variable; `log-metadata` missing | `CLAUDE.md` | No checkov step exists. Scheduled backups succeed, so the variable is repo-scoped. |

The unused `smoke-test-url` inputs on the review workflows are kept on purpose:
removing a declared input breaks every caller that passes it.
