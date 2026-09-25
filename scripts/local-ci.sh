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
# Exit 0 only if every required stage actually ran and passed.
#   required  yamllint, ruff, pytest, workflow invariants, actionlint — these are
#             what CI runs. If one cannot run because its tool is absent, the
#             verdict is INCOMPLETE (exit 1), never PASS: "I did not check" is
#             not "it is fine", and claiming otherwise recreates the local/CI
#             divergence this gate exists to prevent.
#   advisory  zizmor, act — report findings, never block.
#   optional  cdk, aws — only used by the CD stages, skipped when absent.

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
  --fast              skip zizmor and actionlint, as an explicit opt-out
                      (they count as skipped, not missing, so PASS is possible)
  --cdk-project DIR   also run the CD-side checks against a CDK project
  --act               execute self-test.yml locally under `act` (needs Docker)
  --format github     emit ::error/::warning annotations from the stages that
                      support them (yamllint, ruff, invariants); pytest output
                      stays plain text. Exit codes are unaffected.
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
root="$(git rev-parse --show-toplevel)"
# This gate runs against the WORKING TREE, not the staged index. Auto-stashing
# to isolate the index can lose work when a hook is interrupted, so instead we
# say so plainly when the two differ — then a green gate is not misread as
# "exactly what I am committing is green".
if ! git diff --quiet; then
  printf 'local-ci: note — unstaged changes present, so this gate checked the working tree, not just what is staged.\n' >&2
fi
exec "$root/scripts/local-ci.sh" --quiet
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

FAILED=() PASSED=() SKIPPED=() ADVISORY=() MISSING=()
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

# Two kinds of not-run, and conflating them was a real bug: the gate printed
# "PASS — Safe to commit" having never run yamllint or actionlint, both of which
# CI *does* run. A missing optional tool is fine; a missing required one means
# the gate did not actually run, and it must not claim otherwise.
#
# skip     — optional stage (zizmor, act, cdk) or an explicit --fast opt-out.
#            Verdict stays PASS.
# missing  — required stage whose tool is absent. Verdict becomes INCOMPLETE and
#            the exit code is non-zero, because "I didn't check" is not "it's fine".
skip() {
  SKIPPED+=("$1")
  say "${DIM}∅ $1 — $2${RESET}"
}

missing() {
  MISSING+=("$1")
  printf '%s⚠%s %s — required stage not run: %s\n' "$YELLOW" "$RESET" "$1" "$2"
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
  # The format --check stage below will now pass by construction, so flag that
  # files on disk changed and are not staged — otherwise PASS reads as "nothing
  # needed doing".
  if ! git diff --quiet -- scripts/ tests/ 2>/dev/null; then
    say "${YELLOW}  note:${RESET} --fix rewrote files; they are unstaged. Review with: git diff scripts/ tests/"
  fi
fi

# yamllint is a hard dependency (pinned in requirements-dev.txt and run by CI),
# not an optional extra — its absence is `missing`, not `skip`.
if have yamllint; then
  # --strict so warning-level findings exit non-zero too. Without it a
  # warning exits 0 and run() discards the output — the same muted-findings
  # shape as B-1/B-2, just inside a third-party tool's pass contract.
  YAMLLINT_ARGS=(-c .yamllint.yml --strict)
  [ "$FORMAT" = "github" ] && YAMLLINT_ARGS+=(-f github)
  run "yamllint" required yamllint "${YAMLLINT_ARGS[@]}" .github/
else
  missing "yamllint" "pip install -r requirements-dev.txt"
fi

# Same `python3 -m` reasoning as pytest below, and it bit harder here: a
# PATH-shadowed ruff 0.15 passed locally while CI's pinned 0.16 failed on 22
# findings, because the two ship different default rule sets. ruff.toml now
# declares the rules; this makes sure the pinned binary is the one applying them.
if python3 -c 'import ruff' 2>/dev/null; then
  RUFF_ARGS=()
  [ "$FORMAT" = "github" ] && RUFF_ARGS+=(--output-format=github)
  run "ruff check" required python3 -m ruff check "${RUFF_ARGS[@]}" scripts/ tests/
  run "ruff format --check" required python3 -m ruff format --check scripts/ tests/
else
  missing "ruff" "pip install -r requirements-dev.txt"
fi

# Invoke via `python3 -m` so the tests run against the same interpreter that
# has boto3 installed — a bare `pytest` shim can resolve to a different one.
if python3 -c 'import pytest' 2>/dev/null; then
  run "pytest" required python3 -m pytest tests/ -q
else
  missing "pytest" "pip install -r requirements-dev.txt"
fi

# --fail-on-warn is unconditional. Without it, WF004 (mutable internal @main),
# WF007 (forgeable GITHUB_OUTPUT delimiter), WF008 and WF011 were advisory-only
# in the mode CI actually runs, so a brand-new violation of any of them passed
# the required check while the README claimed new violations fail. Baselined
# findings are exempt by fingerprint regardless of severity, so this does not
# resurrect the 25 accepted ones. --strict additionally ignores the baseline.
INV_ARGS=(--format "$FORMAT" --fail-on-warn)
[ "$STRICT" = 1 ] && INV_ARGS+=(--strict)
run "workflow invariants" required \
  python3 scripts/check_workflow_invariants.py "${INV_ARGS[@]}"

# Pinned for the same reason ruff is: a linter whose version differs between
# local and CI enforces different rules in each. We cannot force the version of
# an actionlint already on PATH, but we can say so out loud when it differs from
# what CI installs, rather than letting the drift be silent.
ACTIONLINT_VERSION="v1.7.7"

if [ "$FAST" = 1 ]; then
  # Explicit opt-out; counts as skipped, so PASS stays reachable.
  skip "actionlint" "--fast"
elif have actionlint; then
  actual="$(actionlint --version 2>/dev/null | head -1)"
  case "$actual" in
    "${ACTIONLINT_VERSION#v}"|"$ACTIONLINT_VERSION") : ;;
    *) say "${YELLOW}  note:${RESET} actionlint on PATH is ${actual:-unknown}, CI uses ${ACTIONLINT_VERSION} — findings may differ" ;;
  esac
  run "actionlint" required actionlint -no-color -shellcheck= .github/workflows/*.yml
elif have go; then
  say "${DIM}  installing actionlint (one-off, via go install)...${RESET}"
  if GOBIN="$(go env GOPATH)/bin" go install \
       "github.com/rhysd/actionlint/cmd/actionlint@$ACTIONLINT_VERSION" >/dev/null 2>&1; then
    export PATH="$(go env GOPATH)/bin:$PATH"
    run "actionlint" required actionlint -no-color -shellcheck= .github/workflows/*.yml
  else
    missing "actionlint" "go install github.com/rhysd/actionlint/cmd/actionlint@$ACTIONLINT_VERSION"
  fi
else
  missing "actionlint" "go install github.com/rhysd/actionlint/cmd/actionlint@$ACTIONLINT_VERSION"
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
    # --fail-on-warn for the same reason the CI-side stage has it and the
    # analyzer stage dropped --no-fail-on-public-access: warn-only findings would
    # exit 0, and `run` discards the output of anything exiting 0, so WF004 and
    # WF007 against a caller repo vanished behind a green tick. `advisory` is what
    # keeps this from blocking; the exit code is what makes it visible.
    run "caller-repo invariants" advisory \
      python3 scripts/check_workflow_invariants.py --path "$CDK_ABS" --strict --fail-on-warn

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
      #
      # Deliberately NOT --no-fail-on-public-access. That flag makes the script
      # exit 0 when it finds public access, and `run` prints ✓ and discards the
      # output of anything that exits 0 — so detected public access rendered as
      # an affirmative green tick with the findings thrown away. `advisory`
      # already guarantees this stage cannot block the gate; a non-zero exit is
      # exactly what makes the advisory path *show* the findings.
      if have aws && aws sts get-caller-identity >/dev/null 2>&1; then
        run "access analyzer" advisory \
          python3 scripts/check_no_public_access.py --template-dir "$OUT"
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
[ "${#MISSING[@]}" -gt 0 ] && printf '  missing  %d  %s(%s)%s\n' \
  "${#MISSING[@]}" "$YELLOW" "$(IFS=', '; echo "${MISSING[*]}")" "$RESET"
[ "${#FAILED[@]}" -gt 0 ] && printf '  failed   %d  %s(%s)%s\n' \
  "${#FAILED[@]}" "$RED" "$(IFS=', '; echo "${FAILED[*]}")" "$RESET"

if [ "${#FAILED[@]}" -gt 0 ]; then
  printf '\n%sFAIL%s — %ds. Fix the above before committing.\n' "$RED" "$RESET" "$ELAPSED"
  exit 1
fi
# A required stage that never ran is not a pass. Saying "Safe to commit" here
# would reproduce exactly the local/CI divergence this gate exists to prevent:
# CI installs these tools and will run those stages whether or not you did.
if [ "${#MISSING[@]}" -gt 0 ]; then
  printf '\n%sINCOMPLETE%s — %ds. %d required stage(s) never ran; CI will still run them.\n' \
    "$YELLOW" "$RESET" "$ELAPSED" "${#MISSING[@]}"
  printf 'Install the missing tools (see hints above). --fast opts out of actionlint\nand zizmor only; yamllint, ruff and pytest all come from requirements-dev.txt.\n'
  exit 1
fi
printf '\n%sPASS%s — %ds. Safe to commit.\n' "$GREEN" "$RESET" "$ELAPSED"
exit 0
