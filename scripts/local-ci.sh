#!/usr/bin/env bash
# local-ci.sh — run this repo's CI gate locally, before committing.
#
# The CI stages mirror .github/workflows/self-test.yml so that a green run here
# means a green run there. The CD stages are opt-in and need a target: there is
# no way to genuinely test a deploy locally (it needs AWS and a CDK app), so
# what they do instead is run the *checks* that cdk-review would run against a
# real synthesized template tree.
#
#   ./scripts/local-ci.sh                      # the CI gate (~5s)
#   ./scripts/local-ci.sh --fix                # ...and auto-format first
#   ./scripts/local-ci.sh --strict             # show the baselined work list
#   ./scripts/local-ci.sh --cdk-project ../bitwarden-cdk
#   ./scripts/local-ci.sh --install-hook       # wire up as a pre-commit hook
#
# Exit 0 only if every required stage passed. Advisory stages report but never
# block. Missing optional tools SKIP with an install hint rather than failing,
# so a fresh clone is usable immediately.

# pipefail only, deliberately no `-u`: under `set -u`, bash 3.2 — still
# /bin/bash on macOS — treats `${#arr[@]}` on an empty array as an unbound
# variable and aborts, which the summary block would hit on every clean run.
# Every variable here is explicitly initialised, so `-u` buys little.
# No `-e` either: `run` inspects each stage's exit code itself.
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

# --- options ---------------------------------------------------------------
FAST=0 FIX=0 STRICT=0 WANT_ACT=0 CDK_PROJECT="" QUIET=0
FORMAT="text"

usage() {
  # Print the header comment block: every leading # line after the shebang,
  # stopping at the first line of actual code.
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
    "${BASH_SOURCE[0]}"
  cat <<'EOF'

Options:
  --fix               auto-fix what is mechanically fixable (ruff format)
  --strict            invariants: ignore the baseline and show every finding
  --fast              skip the slower advisory stages (zizmor)
  --cdk-project DIR   also run the CD-side checks against a CDK project
  --act               execute self-test.yml locally under `act` (needs Docker)
  --format github     emit ::error/::warning annotations (for use in CI)
  --install-hook      install this script as .git/hooks/pre-commit
  --quiet             only print the summary and failures
  -h, --help          this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --fix) FIX=1 ;;
    --strict) STRICT=1 ;;
    --fast) FAST=1 ;;
    --act) WANT_ACT=1 ;;
    --quiet) QUIET=1 ;;
    --cdk-project) CDK_PROJECT="${2:-}"; shift ;;
    --format) FORMAT="${2:-text}"; shift ;;
    --install-hook)
      hook="$REPO_ROOT/.git/hooks/pre-commit"
      mkdir -p "$(dirname "$hook")"
      cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# Installed by scripts/local-ci.sh --install-hook
exec "$(git rev-parse --show-toplevel)/scripts/local-ci.sh" --quiet
HOOK
      chmod +x "$hook"
      echo "Installed pre-commit hook → $hook"
      echo "Bypass a single commit with: git commit --no-verify"
      exit 0
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# --- output ----------------------------------------------------------------
if [ -t 1 ] && [ "$FORMAT" = "text" ]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; DIM=""; RESET=""
fi

FAILED=() PASSED=() SKIPPED=() ADVISORY=()
START=$SECONDS

say() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
hdr() { say ""; say "${BOLD}── $* ${RESET}"; }

# run <label> <required|advisory> <cmd...>
# Output is shown only when a stage fails or is advisory — a passing gate should
# be four lines, not four hundred.
run() {
  local label="$1" mode="$2"; shift 2
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    PASSED+=("$label")
    say "${GREEN}✓${RESET} $label"
  elif [ "$mode" = advisory ]; then
    ADVISORY+=("$label")
    say "${YELLOW}!${RESET} $label ${DIM}(advisory — does not block)${RESET}"
    printf '%s\n' "$out" | sed 's/^/    /'
  else
    FAILED+=("$label")
    printf '%s✗%s %s\n' "$RED" "$RESET" "$label"
    printf '%s\n' "$out" | sed 's/^/    /'
  fi
  return 0
}

skip() {
  SKIPPED+=("$1")
  say "${DIM}∅ $1 — $2${RESET}"
}

have() { command -v "$1" >/dev/null 2>&1; }

# Run a command in another directory. `env -C` is GNU-only; a subshell works
# on macOS too, which is where half the devs on this repo will be.
in_dir() { local d="$1"; shift; ( cd "$d" && "$@" ); }

# zizmor prints a source excerpt per finding, which is far too much for a
# pre-commit gate — and several of its rules (notably unpinned-uses) duplicate
# invariants WF003/WF004, which already track those with a baseline. Collapse to
# one line per rule with a count, so a genuinely new rule stands out.
zizmor_brief() {
  local out rc
  out="$(zizmor --offline --persona=regular --format plain .github/ 2>&1)"; rc=$?
  printf '%s\n' "$out" \
    | grep -oE '^(error|warning|help)\[[a-z-]+\]' \
    | sort | uniq -c | sort -rn \
    | awk '{ printf "  %s ×%s\n", $2, $1 }'
  printf '%s\n' "$out" | grep -E '^[0-9]+ findings' | sed 's/^/  /'
  echo "  (detail: zizmor --offline --persona=auditor .github/)"
  return $rc
}

# ===========================================================================
# CI stages — these mirror .github/workflows/self-test.yml
# ===========================================================================
hdr "CI"

if [ "$FIX" = 1 ] && python3 -c 'import ruff' 2>/dev/null; then
  run "ruff format (--fix)" required python3 -m ruff format scripts/ tests/
fi

if have yamllint; then
  run "yamllint" required yamllint -c .yamllint.yml .github/
else
  skip "yamllint" "pip install -r requirements-dev.txt"
fi

# Same `python3 -m` reasoning as pytest below, and it bit harder here: a
# PATH-shadowed ruff 0.15 passed locally while CI's pinned 0.16 failed on 22
# findings, because the two ship different default rule sets. ruff.toml now
# declares the rules; this makes sure the pinned binary is the one applying them.
if python3 -c 'import ruff' 2>/dev/null; then
  run "ruff check" required python3 -m ruff check scripts/ tests/
  run "ruff format --check" required python3 -m ruff format --check scripts/ tests/
elif have ruff; then
  run "ruff check" required ruff check scripts/ tests/
  run "ruff format --check" required ruff format --check scripts/ tests/
else
  skip "ruff" "pip install -r requirements-dev.txt"
fi

# Invoke via `python3 -m` so the tests run against the same interpreter that
# has boto3 installed — a bare `pytest` shim can resolve to a different one.
if python3 -c 'import pytest' 2>/dev/null; then
  run "pytest" required python3 -m pytest tests/ -q
else
  skip "pytest" "pip install -r requirements-dev.txt"
fi

INV_ARGS=(--format "$FORMAT")
[ "$STRICT" = 1 ] && INV_ARGS+=(--strict --fail-on-warn)
run "workflow invariants" required \
  python3 scripts/check_workflow_invariants.py "${INV_ARGS[@]}"

if have actionlint; then
  run "actionlint" required actionlint -no-color -shellcheck= .github/workflows/*.yml
elif have go && [ "$FAST" = 0 ]; then
  say "${DIM}  installing actionlint (one-off, via go install)...${RESET}"
  if GOBIN="$(go env GOPATH)/bin" go install \
       github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 >/dev/null 2>&1; then
    export PATH="$(go env GOPATH)/bin:$PATH"
    run "actionlint" required actionlint -no-color -shellcheck= .github/workflows/*.yml
  else
    skip "actionlint" "go install github.com/rhysd/actionlint/cmd/actionlint@latest"
  fi
else
  skip "actionlint" "go install github.com/rhysd/actionlint/cmd/actionlint@latest"
fi

# zizmor is advisory: the repo currently has known findings (see
# docs/reviews/2026-07-25-*.md and the invariants baseline), so a non-zero exit
# here is expected and must not block a commit.
if [ "$FAST" = 1 ]; then
  skip "zizmor" "--fast"
elif have zizmor; then
  run "zizmor" advisory zizmor_brief
else
  skip "zizmor" "pip install zizmor"
fi

# ===========================================================================
# CD stages — opt-in, need a target
# ===========================================================================
if [ -n "$CDK_PROJECT" ]; then
  hdr "CD — against $CDK_PROJECT"
  if [ ! -d "$CDK_PROJECT" ]; then
    FAILED+=("cdk-project path")
    printf '%s✗%s cdk-project not found: %s\n' "$RED" "$RESET" "$CDK_PROJECT"
  else
    CDK_ABS="$(cd "$CDK_PROJECT" && pwd)"
    OUT="$CDK_ABS/cdk.out"

    # Mirror the caller-repo invariants too — the trigger-convention and
    # secrets checks are most useful pointed at a caller.
    run "caller-repo invariants" advisory \
      python3 scripts/check_workflow_invariants.py --path "$CDK_ABS" --strict

    if have cdk; then
      run "cdk synth" required in_dir "$CDK_ABS" cdk synth --quiet
    else
      skip "cdk synth" "npm i -g aws-cdk (or reuse an existing cdk.out)"
    fi

    if [ -d "$OUT" ]; then
      run "bucket names (source)" required \
        python3 scripts/validate_bucket_names.py --path "$CDK_ABS"
      run "bucket names (templates)" required \
        python3 scripts/validate_bucket_names.py --template-dir "$OUT"
      # check_no_public_access calls the Access Analyzer API, so it needs real
      # credentials. Probe first rather than emitting a traceback.
      if have aws && aws sts get-caller-identity >/dev/null 2>&1; then
        run "access analyzer" advisory \
          python3 scripts/check_no_public_access.py \
            --template-dir "$OUT" --no-fail-on-public-access
      else
        skip "access analyzer" "no usable AWS credentials (needs a real API call)"
      fi
    else
      skip "template checks" "no cdk.out at $OUT"
    fi
  fi
fi

if [ "$WANT_ACT" = 1 ]; then
  hdr "act — executing self-test.yml locally"
  if ! have act; then
    skip "act" "https://github.com/nektos/act#installation"
  elif ! docker info >/dev/null 2>&1; then
    skip "act" "Docker daemon not reachable"
  else
    # Only self-test.yml has a real trigger; the rest are workflow_call, which
    # act cannot invoke standalone.
    run "act pull_request" advisory \
      act pull_request -W .github/workflows/self-test.yml --container-architecture linux/amd64
  fi
fi

# ===========================================================================
# Summary
# ===========================================================================
ELAPSED=$((SECONDS - START))
printf '\n%s── summary ──%s\n' "$BOLD" "$RESET"
printf '  passed   %d\n' "${#PASSED[@]}"
[ "${#ADVISORY[@]}" -gt 0 ] && printf '  advisory %d  %s(%s)%s\n' \
  "${#ADVISORY[@]}" "$YELLOW" "$(IFS=', '; echo "${ADVISORY[*]}")" "$RESET"
[ "${#SKIPPED[@]}" -gt 0 ] && printf '  skipped  %d  %s(%s)%s\n' \
  "${#SKIPPED[@]}" "$DIM" "$(IFS=', '; echo "${SKIPPED[*]}")" "$RESET"
[ "${#FAILED[@]}" -gt 0 ] && printf '  failed   %d  %s(%s)%s\n' \
  "${#FAILED[@]}" "$RED" "$(IFS=', '; echo "${FAILED[*]}")" "$RESET"

if [ "${#FAILED[@]}" -gt 0 ]; then
  printf '\n%sFAIL%s — %ds. Fix the above before committing.\n' "$RED" "$RESET" "$ELAPSED"
  exit 1
fi
printf '\n%sPASS%s — %ds. Safe to commit.\n' "$GREEN" "$RESET" "$ELAPSED"
exit 0
