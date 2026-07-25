"""
Tests for scripts/check_workflow_invariants.py.

The point of these is negative coverage: it's easy to write a checker that
passes on a clean tree and would also pass on a broken one. Every check gets a
crafted violation that it must catch, plus a clean counterpart it must not flag.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_workflow_invariants.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_workflow_invariants as ci  # noqa: E402

PINNED = "actions/checkout@" + "a" * 40


def wf(body: str) -> str:
    """Dedent at definition time so that .replace() targets in the tests match
    the exact text that gets written — not an indented pre-image of it."""
    return textwrap.dedent(body).lstrip()


def write_workflow(root: Path, name: str, body: str) -> Path:
    d = root / ".github" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body if body[:1] not in " \n" else wf(body), encoding="utf-8")
    return p


def findings_for(root: Path) -> list[ci.Finding]:
    findings, errors = ci.collect(root)
    assert not errors, errors
    return findings


def checks_hit(root: Path) -> set[str]:
    return {f.check for f in findings_for(root)}


# A workflow that violates nothing. Every negative test starts from this and
# breaks exactly one thing, so a test failure points at one check.
CLEAN = wf(f"""
    name: Clean
    on:
      pull_request:
        branches: [main]
    jobs:
      build:
        runs-on: ubuntu-latest
        timeout-minutes: 10
        permissions:
          contents: read
        concurrency:
          group: clean-${{{{ github.ref }}}}
          cancel-in-progress: true
        steps:
          - uses: {PINNED}  # v5.0.0
            with:
              persist-credentials: false
          - name: Do the thing
            run: echo hello
""")


# Exact fragments of the final (dedented) CLEAN text. Named so a change to
# CLEAN's shape breaks loudly in one place instead of silently no-op'ing a
# .replace() and leaving the test passing for the wrong reason.
PC_LINE = "          persist-credentials: false\n"
CONCURRENCY_BLOCK = (
    "    concurrency:\n"
    "      group: clean-${{ github.ref }}\n"
    "      cancel-in-progress: true\n"
)
PERMISSIONS_BLOCK = "    permissions:\n      contents: read\n"
RUN_LINE = "        run: echo hello\n"
TIMEOUT_LINE = "    timeout-minutes: 10\n"
PR_TRIGGER = "  pull_request:\n    branches: [main]\n"


def swap(old: str, new: str) -> str:
    """CLEAN with `old` replaced by `new`, proving the swap took effect."""
    assert old in CLEAN, f"fragment not present in CLEAN: {old!r}"
    return CLEAN.replace(old, new)


def test_clean_workflow_has_no_findings(tmp_path):
    write_workflow(tmp_path, "clean.yml", CLEAN)
    assert findings_for(tmp_path) == []


def test_this_repo_has_no_blocking_findings_outside_baseline():
    """The real gate: the committed tree must be green through the CLI."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_baseline_has_no_stale_entries():
    """Every baselined fingerprint must still correspond to a real finding, and
    no entry may accept more findings than actually exist.

    Without the first half, fixing an issue leaves a dead entry that would
    silently re-accept the problem if it came back. Without the second, an
    inflated `count` is a blank cheque for future duplicates.
    """
    accepted, _ = ci.load_baseline(
        REPO_ROOT / ".github" / "workflow-invariants-baseline.yml"
    )
    live: dict[str, int] = {}
    for f in findings_for(REPO_ROOT):
        live[f.fingerprint] = live.get(f.fingerprint, 0) + 1
    stale = set(accepted) - set(live)
    assert stale == set(), f"baseline entries match no finding: {sorted(stale)}"
    over = {fp: (n, live[fp]) for fp, n in accepted.items() if n > live[fp]}
    assert over == {}, f"baseline accepts more than exist (accepted, live): {over}"


# --- WF001 timeout-minutes --------------------------------------------------


def test_wf001_flags_job_without_timeout(tmp_path):
    write_workflow(tmp_path, "w.yml", swap(TIMEOUT_LINE, ""))
    assert "WF001" in checks_hit(tmp_path)


def test_wf001_skips_job_that_only_calls_a_reusable_workflow(tmp_path):
    write_workflow(
        tmp_path,
        "w.yml",
        """
        name: Caller
        on:
          pull_request:
            branches: [main]
        jobs:
          call:
            uses: ./.github/workflows/other.yml
        """,
    )
    assert "WF001" not in checks_hit(tmp_path)


# --- WF002 persist-credentials ---------------------------------------------


@pytest.mark.parametrize(
    "replacement",
    [
        f"      - uses: {PINNED}\n",  # no with: block at all
        f"      - uses: {PINNED}\n        with:\n          persist-credentials: true\n",
    ],
)
def test_wf002_flags_checkout_leaving_credentials(tmp_path, replacement):
    body = CLEAN.replace(
        f"      - uses: {PINNED}  # v5.0.0\n"
        "        with:\n"
        "          persist-credentials: false\n",
        replacement,
    )
    write_workflow(tmp_path, "w.yml", body)
    assert "WF002" in checks_hit(tmp_path)


# --- WF003 / WF004 pinning -------------------------------------------------


def test_wf003_flags_unpinned_third_party_action(tmp_path):
    write_workflow(tmp_path, "w.yml", CLEAN.replace(PINNED, "actions/checkout@v5"))
    assert "WF003" in checks_hit(tmp_path)


def test_wf004_flags_internal_action_at_main(tmp_path):
    write_workflow(
        tmp_path,
        "w.yml",
        CLEAN.replace(PINNED, "Specter099/.github/.github/actions/setup-cdk@main"),
    )
    hit = checks_hit(tmp_path)
    assert "WF004" in hit and "WF003" not in hit


def test_wf004_flags_cross_repo_checkout_without_ref(tmp_path):
    write_workflow(
        tmp_path,
        "w.yml",
        swap(PC_LINE, PC_LINE + "          repository: Specter099/.github\n"),
    )
    assert "WF004" in checks_hit(tmp_path)


def test_wf004_accepts_cross_repo_checkout_with_ref(tmp_path):
    write_workflow(
        tmp_path,
        "w.yml",
        swap(
            PC_LINE,
            PC_LINE
            + "          repository: Specter099/.github\n"
            + f"          ref: {'b' * 40}\n",
        ),
    )
    assert "WF004" not in checks_hit(tmp_path)


def test_local_action_reference_is_not_flagged(tmp_path):
    write_workflow(tmp_path, "w.yml", CLEAN.replace(PINNED, "./.github/actions/thing"))
    hit = checks_hit(tmp_path)
    assert "WF003" not in hit and "WF004" not in hit


# --- WF005 undeclared secrets ---------------------------------------------


REUSABLE = wf("""
    name: Reusable
    on:
      workflow_call:
        inputs:
          region:
            type: string
            default: us-east-1
    jobs:
      go:
        runs-on: ubuntu-latest
        timeout-minutes: 5
        permissions:
          contents: read
        steps:
          - name: Use it
            env:
              R: ${{ inputs.region }}
              ROLE: ${{ secrets.AWS_ROLE_ARN }}
            run: echo "$R $ROLE"
""")


def test_wf005_flags_undeclared_secret(tmp_path):
    write_workflow(tmp_path, "r.yml", REUSABLE)
    assert "WF005" in checks_hit(tmp_path)


def test_wf005_accepts_declared_secret(tmp_path):
    declared = """
        name: Reusable
        on:
          workflow_call:
            secrets:
              AWS_ROLE_ARN:
                required: true
        jobs:
          go:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - name: Use it
                env:
                  ROLE: ${{ secrets.AWS_ROLE_ARN }}
                run: echo "$ROLE"
    """
    write_workflow(tmp_path, "r.yml", declared)
    assert "WF005" not in checks_hit(tmp_path)


def test_wf005_ignores_github_token(tmp_path):
    body = """
        name: Reusable
        on:
          workflow_call:
        jobs:
          go:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - name: Use it
                env:
                  T: ${{ secrets.GITHUB_TOKEN }}
                run: echo "$T"
    """
    write_workflow(tmp_path, "r.yml", body)
    assert "WF005" not in checks_hit(tmp_path)


# --- WF006 / WF014 triggers ----------------------------------------------


def test_wf006_flags_pull_request_target(tmp_path):
    write_workflow(
        tmp_path, "w.yml", CLEAN.replace("  pull_request:", "  pull_request_target:")
    )
    assert "WF006" in checks_hit(tmp_path)


def test_wf014_flags_pull_request_and_push_main(tmp_path):
    write_workflow(
        tmp_path,
        "w.yml",
        swap(PR_TRIGGER, PR_TRIGGER + "  push:\n    branches: [main]\n"),
    )
    assert "WF014" in checks_hit(tmp_path)


def test_wf014_allows_push_main_alone(tmp_path):
    write_workflow(
        tmp_path,
        "w.yml",
        swap(PR_TRIGGER, "  push:\n    branches: [main]\n"),
    )
    assert "WF014" not in checks_hit(tmp_path)


# --- WF007 heredoc delimiter ---------------------------------------------


def test_wf007_flags_fixed_eof_delimiter(tmp_path):
    body = swap(
        RUN_LINE, '        run: |\n          echo "diff<<EOF" >> "$GITHUB_OUTPUT"\n'
    )
    write_workflow(tmp_path, "w.yml", body)
    assert "WF007" in checks_hit(tmp_path)


def test_wf007_accepts_randomised_delimiter(tmp_path):
    body = swap(
        RUN_LINE,
        "        run: |\n"
        '          d="EOF_$(openssl rand -hex 16)"\n'
        '          echo "diff<<$d" >> "$GITHUB_OUTPUT"\n',
    )
    write_workflow(tmp_path, "w.yml", body)
    assert "WF007" not in checks_hit(tmp_path)


# --- WF008 concurrency ---------------------------------------------------


def test_wf008_flags_review_workflow_without_concurrency(tmp_path):
    body = swap(CONCURRENCY_BLOCK, "")
    write_workflow(tmp_path, "cdk-review.yml", body)
    assert "WF008" in checks_hit(tmp_path)


def test_wf008_ignores_non_check_workflows(tmp_path):
    body = swap(CONCURRENCY_BLOCK, "")
    write_workflow(tmp_path, "cdk-deploy.yml", body)
    assert "WF008" not in checks_hit(tmp_path)


# --- WF009 script injection ----------------------------------------------


def test_wf009_flags_input_interpolated_into_run(tmp_path):
    body = """
        name: Injectable
        on:
          workflow_call:
            inputs:
              stacks:
                type: string
                default: "--all"
        jobs:
          go:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - name: Deploy
                run: cdk deploy ${{ inputs.stacks }}
    """
    write_workflow(tmp_path, "w.yml", body)
    assert "WF009" in checks_hit(tmp_path)


def test_wf009_flags_pr_title_interpolated_into_run(tmp_path):
    body = CLEAN.replace(
        "        run: echo hello\n",
        "        run: echo ${{ github.event.pull_request.title }}\n",
    )
    write_workflow(tmp_path, "w.yml", body)
    assert "WF009" in checks_hit(tmp_path)


def test_wf009_accepts_the_env_indirection_pattern(tmp_path):
    """The fix this org uses everywhere: bind to env:, quote in the shell."""
    body = """
        name: Safe
        on:
          workflow_call:
            inputs:
              stacks:
                type: string
                default: "--all"
        jobs:
          go:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - name: Deploy
                env:
                  CDK_STACKS: ${{ inputs.stacks }}
                run: cdk deploy "$CDK_STACKS"
    """
    write_workflow(tmp_path, "w.yml", body)
    assert "WF009" not in checks_hit(tmp_path)


# --- WF010 permissions ---------------------------------------------------


def test_wf010_flags_job_without_permissions(tmp_path):
    write_workflow(tmp_path, "w.yml", swap(PERMISSIONS_BLOCK, ""))
    assert "WF010" in checks_hit(tmp_path)


def test_wf010_accepts_workflow_level_permissions(tmp_path):
    body = swap(PERMISSIONS_BLOCK, "").replace(
        "jobs:\n", "permissions:\n  contents: read\njobs:\n"
    )
    write_workflow(tmp_path, "w.yml", body)
    assert "WF010" not in checks_hit(tmp_path)


# --- WF011 unused inputs -------------------------------------------------


def test_wf011_flags_unreferenced_input(tmp_path):
    body = """
        name: Vestigial
        on:
          workflow_call:
            inputs:
              never-used:
                type: string
                default: ""
        jobs:
          go:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - run: echo hi
    """
    write_workflow(tmp_path, "w.yml", body)
    assert "WF011" in checks_hit(tmp_path)


def test_wf011_recognises_composite_env_var_reference(tmp_path):
    """Composite actions can read an input as INPUT_FOO_BAR rather than
    ${{ inputs.foo-bar }} — that still counts as a reference."""
    d = tmp_path / ".github" / "actions" / "thing"
    d.mkdir(parents=True)
    (d / "action.yml").write_text(
        textwrap.dedent(
            """
            name: Thing
            description: t
            inputs:
              log-dir:
                description: d
                default: ""
            runs:
              using: composite
              steps:
                - shell: bash
                  run: echo "$INPUT_LOG_DIR"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    write_workflow(tmp_path, "w.yml", CLEAN)
    assert "WF011" not in checks_hit(tmp_path)


# --- WF012 README examples ----------------------------------------------


def test_wf012_flags_readme_example_with_undeclared_input(tmp_path):
    write_workflow(
        tmp_path,
        "python-ci.yml",
        """
        name: Python CI
        on:
          workflow_call:
            inputs:
              python-versions:
                type: string
                default: '["3.12"]'
        jobs:
          ci:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - name: Go
                env:
                  V: ${{ inputs.python-versions }}
                run: echo "$V"
        """,
    )
    (tmp_path / "README.md").write_text(
        textwrap.dedent(
            """
            ```yaml
            jobs:
              ci:
                uses: Specter099/.github/.github/workflows/python-ci.yml@main
                with:
                  python-version: "3.12"
            ```
            """
        ),
        encoding="utf-8",
    )
    hits = [f for f in findings_for(tmp_path) if f.check == "WF012"]
    assert hits and "python-version" in hits[0].detail


def test_wf012_accepts_correct_readme_example(tmp_path):
    write_workflow(
        tmp_path,
        "python-ci.yml",
        """
        name: Python CI
        on:
          workflow_call:
            inputs:
              python-versions:
                type: string
                default: '["3.12"]'
        jobs:
          ci:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - name: Go
                env:
                  V: ${{ inputs.python-versions }}
                run: echo "$V"
        """,
    )
    (tmp_path / "README.md").write_text(
        textwrap.dedent(
            """
            ```yaml
            jobs:
              ci:
                uses: Specter099/.github/.github/workflows/python-ci.yml@main
                with:
                  python-versions: '["3.12"]'
            ```
            """
        ),
        encoding="utf-8",
    )
    assert "WF012" not in checks_hit(tmp_path)


# --- WF013 no checks in deploy ------------------------------------------


def test_wf013_flags_pytest_in_a_deploy_workflow(tmp_path):
    body = swap(RUN_LINE, "        run: pytest tests/\n")
    write_workflow(tmp_path, "cdk-deploy.yml", body)
    assert "WF013" in checks_hit(tmp_path)


def test_wf013_allows_npm_run_build_in_deploy(tmp_path):
    body = swap(RUN_LINE, "        run: npm run build\n")
    write_workflow(tmp_path, "static-site-deploy.yml", body)
    assert "WF013" not in checks_hit(tmp_path)


def test_wf013_ignores_review_workflows(tmp_path):
    body = swap(RUN_LINE, "        run: pytest tests/\n")
    write_workflow(tmp_path, "cdk-review.yml", body)
    assert "WF013" not in checks_hit(tmp_path)


# --- infrastructure -----------------------------------------------------


def test_quoted_and_bare_on_key_both_parse(tmp_path):
    """PyYAML turns a bare `on:` into the boolean True — both spellings, which
    this repo mixes, must resolve to the same trigger block."""
    bare = ci.triggers({True: {"workflow_call": None}})
    quoted = ci.triggers({"on": {"workflow_call": None}})
    assert "workflow_call" in bare and "workflow_call" in quoted


def test_baseline_suppresses_a_finding(tmp_path):
    write_workflow(tmp_path, "w.yml", CLEAN.replace(PINNED, "actions/checkout@v5"))
    findings = findings_for(tmp_path)
    target = next(f for f in findings if f.check == "WF003")
    baseline = tmp_path / "baseline.yml"
    baseline.write_text(
        f'accepted:\n  - fingerprint: "{target.fingerprint}"\n', encoding="utf-8"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--path",
            str(tmp_path),
            "--baseline",
            str(baseline),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout
    # ...and --strict must ignore the baseline and fail again.
    strict = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--path",
            str(tmp_path),
            "--baseline",
            str(baseline),
            "--strict",
        ],
        capture_output=True,
        text=True,
    )
    assert strict.returncode == 1, strict.stdout


def test_fingerprint_is_stable_across_line_moves(tmp_path):
    """A finding's identity must not include the line number, or an unrelated
    edit above it silently un-baselines it."""
    write_workflow(tmp_path, "w.yml", CLEAN.replace(PINNED, "actions/checkout@v5"))
    before = next(f for f in findings_for(tmp_path) if f.check == "WF003")
    write_workflow(
        tmp_path,
        "w.yml",
        "# a new comment line\n" + CLEAN.replace(PINNED, "actions/checkout@v5"),
    )
    after = next(f for f in findings_for(tmp_path) if f.check == "WF003")
    assert before.fingerprint == after.fingerprint
    assert before.line != after.line


def test_unparseable_yaml_is_a_hard_error(tmp_path):
    write_workflow(tmp_path, "bad.yml", "name: [unclosed\n")
    _findings, errors = ci.collect(tmp_path)
    assert errors


def test_every_check_id_has_a_rationale():
    for cid, (sev, why) in ci.CHECKS.items():
        assert sev in {"error", "warn"}, cid
        assert why and len(why) < 100, cid


# ---------------------------------------------------------------------------
# Regression guards for the bypasses found in adversarial review of PR #138.
# Each of these passed the checker before the fix, so they are the tests that
# would have caught the gap.
# ---------------------------------------------------------------------------


def _caller(uses: str) -> str:
    """A workflow whose only job is a reusable-workflow call."""
    return wf(f"""
        name: Caller
        on:
          pull_request:
            branches: [main]
        jobs:
          call:
            uses: {uses}
            secrets: inherit
    """)


def test_wf003_flags_unpinned_third_party_reusable_workflow_call(tmp_path):
    """Job-level `uses:` was invisible to the step walk, so an unpinned
    third-party reusable workflow — the one composition that can carry
    `secrets: inherit` — passed even under --strict --fail-on-warn."""
    write_workflow(
        tmp_path,
        "c.yml",
        _caller("some-rando/malicious/.github/workflows/pwn.yml@main"),
    )
    hits = [f for f in findings_for(tmp_path) if f.check == "WF003"]
    assert hits, "unpinned third-party reusable workflow call not flagged"
    assert "reusable workflow call" in hits[0].detail


def test_wf004_flags_internal_reusable_workflow_call_at_main(tmp_path):
    write_workflow(
        tmp_path,
        "c.yml",
        _caller("Specter099/.github/.github/workflows/python-ci.yml@main"),
    )
    hit = checks_hit(tmp_path)
    assert "WF004" in hit and "WF003" not in hit


def test_sha_pinned_reusable_workflow_call_is_accepted(tmp_path):
    write_workflow(
        tmp_path, "c.yml", _caller(f"some-org/repo/.github/workflows/x.yml@{'c' * 40}")
    )
    hit = checks_hit(tmp_path)
    assert "WF003" not in hit and "WF004" not in hit


def test_local_reusable_workflow_call_is_accepted(tmp_path):
    write_workflow(tmp_path, "c.yml", _caller("./.github/workflows/other.yml"))
    hit = checks_hit(tmp_path)
    assert "WF003" not in hit and "WF004" not in hit


def _triggers_wf(on_block: str) -> str:
    return wf(f"""
        name: S
        {on_block}
        jobs:
          s:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            permissions:
              contents: read
            steps:
              - run: echo hi
    """)


def test_wf014_flags_bare_push_with_no_branch_filter(tmp_path):
    """`push:` with no filter fires on every branch including main, so it
    double-runs at merge just like `push: {branches: [main]}`."""
    write_workflow(
        tmp_path,
        "s.yml",
        _triggers_wf(
            "on:\n          pull_request:\n            branches: [main]\n          push:"
        ),
    )
    assert "WF014" in checks_hit(tmp_path)


def test_wf014_flags_list_form_triggers(tmp_path):
    """`on: [pull_request, push]` — the list form maps push to None."""
    write_workflow(tmp_path, "s.yml", _triggers_wf("on: [pull_request, push]"))
    assert "WF014" in checks_hit(tmp_path)


def test_wf014_allows_push_restricted_to_other_branches(tmp_path):
    """An explicit filter that excludes main is the legitimate case."""
    write_workflow(
        tmp_path,
        "s.yml",
        _triggers_wf(
            "on:\n          pull_request:\n            branches: [main]\n"
            "          push:\n            branches: [release/**]"
        ),
    )
    assert "WF014" not in checks_hit(tmp_path)


def test_gate_runs_the_checker_with_fail_on_warn():
    """LOAD-BEARING — do not delete as redundant with the class-level test below.

    This asserts the flag sits on the *unconditional* INV_ARGS= line. The
    class-level test generalises across invocations, but an adversarial mutation
    that moved --fail-on-warn into a conditional `INV_ARGS+=(...)` was caught only
    here. The two overlap in what they cover and differ in what they can miss;
    keep both.

    The gate must not leave warn-severity checks advisory.

    WF004 (mutable internal @main) and WF007 (forgeable GITHUB_OUTPUT
    delimiter) are `warn`. Without --fail-on-warn they were advisory-only in
    the mode self-test.yml runs, so a brand-new violation of either passed the
    required check while the README promised new violations fail. This asserts
    the flag is unconditional rather than tied to --strict.
    """
    gate = (REPO_ROOT / "scripts" / "local-ci.sh").read_text(encoding="utf-8")
    inv_line = next(
        line for line in gate.splitlines() if line.strip().startswith("INV_ARGS=(")
    )
    assert "--fail-on-warn" in inv_line, (
        "local-ci.sh must pass --fail-on-warn unconditionally; found: " + inv_line
    )


def test_warn_findings_block_once_fail_on_warn_is_set(tmp_path):
    """End-to-end via the CLI: a warn-only finding is exit 0 by default and
    exit 1 with --fail-on-warn, so the gate's flag is what makes it bite."""
    write_workflow(
        tmp_path,
        "cdk-review.yml",
        swap(PINNED, "Specter099/.github/.github/actions/setup-cdk@main"),
    )
    base = [sys.executable, str(SCRIPT), "--path", str(tmp_path)]
    lenient = subprocess.run(base, capture_output=True, text=True)
    strict = subprocess.run([*base, "--fail-on-warn"], capture_output=True, text=True)
    assert lenient.returncode == 0, lenient.stdout
    assert strict.returncode == 1, strict.stdout


def test_wf014_flags_push_dict_without_a_branches_key(tmp_path):
    """`push:` as a mapping with only a path/tag filter and no `branches:` still
    fires on every branch, main included — a PR touching src/** runs the check,
    then the merge push to main runs it again.

    Distinct from the bare `push:` case above: that one parses to None and takes
    the non-mapping path. This one is a dict whose `branches` is absent, and
    mutation testing showed the bare-push test did not cover it.
    """
    write_workflow(
        tmp_path,
        "s.yml",
        _triggers_wf(
            "on:\n          pull_request:\n            branches: [main]\n"
            "          push:\n            paths: ['src/**']"
        ),
    )
    assert "WF014" in checks_hit(tmp_path)


# --- round-2 regression guards ---------------------------------------------


def test_wf014_allows_tags_only_push(tmp_path):
    """`push: {tags: [...]}` never fires on a branch push, so there is no
    merge-time double-run. This is the common "PR checks + tag release" caller
    layout, and flagging it was a false positive on an error-severity check."""
    write_workflow(
        tmp_path,
        "s.yml",
        _triggers_wf(
            "on:\n          pull_request:\n            branches: [main]\n"
            "          push:\n            tags: ['v*']"
        ),
    )
    assert "WF014" not in checks_hit(tmp_path)


def test_wf014_allows_branches_ignore_main(tmp_path):
    write_workflow(
        tmp_path,
        "s.yml",
        _triggers_wf(
            "on:\n          pull_request:\n            branches: [main]\n"
            "          push:\n            branches-ignore: [main]"
        ),
    )
    assert "WF014" not in checks_hit(tmp_path)


@pytest.mark.parametrize("pattern", ["m*", "**", "mai?", "*"])
def test_wf014_flags_glob_branch_patterns_matching_main(tmp_path, pattern):
    """Branch filters are globs. An exact-string membership test missed
    `branches: ["m*"]`, which does fire on main."""
    write_workflow(
        tmp_path,
        "s.yml",
        _triggers_wf(
            "on:\n          pull_request:\n            branches: [main]\n"
            f"          push:\n            branches: ['{pattern}']"
        ),
    )
    assert "WF014" in checks_hit(tmp_path)


def test_baseline_count_stops_a_duplicate_new_violation(tmp_path):
    """A second, genuinely new violation that happens to share a fingerprint
    with a baselined one must still block.

    Two identical `echo "x<<EOF"` lines in one file produce the same
    check+path+detail, so a set-based baseline absorbed the new one silently.
    """
    body = swap(
        RUN_LINE,
        '        run: |\n          echo "diff<<EOF" >> "$GITHUB_OUTPUT"\n',
    )
    write_workflow(tmp_path, "w.yml", body)
    one = [f for f in findings_for(tmp_path) if f.check == "WF007"]
    assert len(one) == 1
    fp = one[0].fingerprint

    baseline = tmp_path / "b.yml"
    baseline.write_text(f'accepted:\n  - fingerprint: "{fp}"\n', encoding="utf-8")
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--path",
        str(tmp_path),
        "--baseline",
        str(baseline),
        "--fail-on-warn",
    ]

    # One occurrence, budget of one → accepted.
    assert subprocess.run(cmd, capture_output=True, text=True).returncode == 0

    # Add a second identical occurrence → the budget is spent, so it blocks.
    write_workflow(
        tmp_path,
        "w.yml",
        body.replace(
            '          echo "diff<<EOF" >> "$GITHUB_OUTPUT"\n',
            '          echo "diff<<EOF" >> "$GITHUB_OUTPUT"\n'
            '          echo "diff<<EOF" >> "$GITHUB_OUTPUT"\n',
        ),
    )
    assert len([f for f in findings_for(tmp_path) if f.check == "WF007"]) == 2
    second = subprocess.run(cmd, capture_output=True, text=True)
    assert second.returncode == 1, second.stdout

    # An explicit count of 2 accepts both, so the escape hatch still exists.
    baseline.write_text(
        f'accepted:\n  - fingerprint: "{fp}"\n    count: 2\n', encoding="utf-8"
    )
    assert subprocess.run(cmd, capture_output=True, text=True).returncode == 0


# ---------------------------------------------------------------------------
# The gate's advisory contract.
#
# `run()` prints ✓ and DISCARDS the output of anything exiting 0. So a stage
# whose findings matter must signal them with a non-zero exit — otherwise the
# gate renders a detected problem as an affirmative green tick. This bit the
# CD access-analyzer stage, which passed --no-fail-on-public-access (making the
# script exit 0 on violations) while running in advisory mode.
# ---------------------------------------------------------------------------

GATE = (REPO_ROOT / "scripts" / "local-ci.sh").read_text(encoding="utf-8")


def test_no_gate_stage_mutes_a_findings_exit_code():
    """Class-level guard, not instance-level.

    Both blocking bugs in adversarial review were the same shape: a stage that
    reported a real finding while exiting 0, so `run()` printed ✓ and discarded
    the output. It happened twice — once with --no-fail-on-public-access on the
    analyzer stage, then again with a missing --fail-on-warn on the caller-repo
    invariants stage — because the first fix addressed the instance. This asserts
    the property over *every* invocation of either script in the gate.
    """
    muting = {
        "check_no_public_access.py": ("--no-fail-on-public-access", False),
        "check_workflow_invariants.py": ("--fail-on-warn", True),
    }
    lines = GATE.splitlines()

    def effective_args(line: str) -> str:
        """The line's arguments, following one level of array indirection.

        The CI-side stage passes its flags via "${INV_ARGS[@]}", so checking the
        invocation line alone would wrongly report it as unflagged. Resolve any
        referenced array to the lines that build it.
        """
        text = line
        for var in re.findall(r"\$\{(\w+)\[@\]\}", line):
            for candidate in lines:
                stripped = candidate.strip()
                if not stripped.startswith((f"{var}=", f"{var}+=")):
                    continue
                # Only unconditional assignments count. `[ "$X" = 1 ] && VAR+=(…)`
                # and anything indented under an `if` apply on *some* runs, so
                # crediting them here would be fail-open — review demonstrated a
                # mutation that moved the flag into a conditional append and slipped
                # past an earlier version of this check.
                if candidate != stripped:
                    continue  # indented → inside a block
                if "&&" in candidate.split("=", 1)[0] or candidate.lstrip().startswith(
                    "["
                ):
                    continue  # guarded by a test
                text += " " + candidate
        return text

    for script, (flag, required) in muting.items():
        invocations = [
            effective_args(line)
            for line in lines
            if script in line and "python3" in line
        ]
        assert invocations, f"expected the gate to invoke {script}"
        for inv in invocations:
            if required:
                # The invariants checker is silent-on-warn unless told otherwise,
                # so every gate invocation must opt in.
                assert flag in inv, (
                    f"{script} invoked without {flag}; warn-severity findings "
                    f"would exit 0 and be discarded by run(). Line: {inv}"
                )
            else:
                assert flag not in inv, (
                    f"{script} invoked with {flag}; violations would exit 0 and "
                    f"be discarded by run(). Line: {inv}"
                )


def test_gate_does_not_mute_the_access_analyzer_exit_code():
    """--no-fail-on-public-access + advisory run() = green tick over a real
    finding, with the findings text thrown away. `advisory` already guarantees
    the stage cannot block, so the exit code must stay meaningful."""
    analyzer_lines = [
        line for line in GATE.splitlines() if "check_no_public_access.py" in line
    ]
    assert analyzer_lines, "expected the CD stage to invoke check_no_public_access.py"
    invocation = " ".join(analyzer_lines)
    assert "--no-fail-on-public-access" not in invocation, (
        "the gate must not suppress the analyzer's exit code; advisory mode "
        "already prevents it from blocking. Found: " + invocation
    )


def test_advisory_stages_only_show_output_on_nonzero_exit():
    """Pins the run() behaviour the test above depends on, so a future change to
    run() that swallowed non-zero output would surface here rather than silently
    invalidating the reasoning."""
    body = GATE.split("run() {", 1)[1].split("\n}", 1)[0]
    zero_branch = body.split("elif", 1)[0]
    # The rc==0 path must not print captured output...
    assert "$out" not in zero_branch, (
        "run() now prints output on success; the advisory reasoning above needs "
        "revisiting"
    )
    # ...while the advisory path must.
    advisory_branch = body.split("elif", 1)[1].split("else", 1)[0]
    assert "$out" in advisory_branch


def test_check_no_public_access_exit_code_depends_on_the_flag(tmp_path, monkeypatch):
    """The underlying behaviour that made the flag dangerous in an advisory
    stage: with it, violations exit 0; without it, they exit 1."""
    import check_no_public_access as cnpa

    (tmp_path / "S.template.json").write_text(
        json.dumps(
            {
                "Resources": {
                    "BP": {
                        "Type": "AWS::S3::BucketPolicy",
                        "Properties": {
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Principal": "*",
                                        "Action": "s3:GetObject",
                                        "Resource": "arn:aws:s3:::b/*",
                                    }
                                ],
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeAnalyzer:
        def check_no_public_access(self, **_kw):
            return {"result": "FAIL", "reasons": [{"description": "public"}]}

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    monkeypatch.setattr(
        cnpa.boto3,
        "client",
        lambda name, **_kw: FakeSts() if name == "sts" else FakeAnalyzer(),
    )

    def run_with(argv):
        monkeypatch.setattr(
            "sys.argv", ["prog", "--template-dir", str(tmp_path), *argv]
        )
        return cnpa.main()

    assert run_with([]) == 1, "violations must be a non-zero exit by default"
    assert run_with(["--no-fail-on-public-access"]) == 0, (
        "the flag is what makes violations exit 0 — which is why an advisory "
        "stage must not pass it"
    )


# --- round-3 regression guards ---------------------------------------------


@pytest.mark.parametrize(
    "branches,expect_flag",
    [
        ("['**', '!main']", False),  # negation excludes main → no double-run
        ("['!main']", False),
        ("['**']", True),
        ("['release/**', '!release/old']", False),  # main matches nothing
        ("['!dev', '**']", True),  # later positive re-includes main
    ],
)
def test_wf014_honours_negated_branch_patterns(tmp_path, branches, expect_flag):
    """GitHub branch filters support `!` negation with later entries overriding
    earlier ones, so ['**', '!main'] means every branch except main."""
    write_workflow(
        tmp_path,
        "s.yml",
        _triggers_wf(
            "on:\n          pull_request:\n            branches: [main]\n"
            f"          push:\n            branches: {branches}"
        ),
    )
    assert ("WF014" in checks_hit(tmp_path)) is expect_flag


@pytest.mark.parametrize(
    "content",
    [
        "accepted: [ unclosed",  # not valid YAML
        "- just\n- a\n- list\n",  # top level not a mapping
        'accepted:\n  - fingerprint: "x"\n    count: banana\n',  # count not an int
        'accepted:\n  - fingerprint: "x"\n    count: 0\n',  # a zero budget
        'accepted:\n  - fingerprint: "x"\n    count: -5\n',  # negative
    ],
)
def test_malformed_baseline_exits_two_without_a_traceback(tmp_path, content):
    """A broken baseline must fail closed *and* legibly. Exit 2 is the
    documented "could not run"; a traceback is correct-but-unreadable, and
    silently ignoring the file would accept nothing while burying the reason."""
    write_workflow(tmp_path, "w.yml", CLEAN)
    baseline = tmp_path / "b.yml"
    baseline.write_text(content, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--path",
            str(tmp_path),
            "--baseline",
            str(baseline),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "error:" in proc.stderr, proc.stderr


def test_absent_baseline_is_not_an_error(tmp_path):
    """A caller repo with no baseline at all is the normal case, not a failure —
    every finding is simply unaccepted."""
    write_workflow(tmp_path, "w.yml", CLEAN)
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--path",
            str(tmp_path),
            "--baseline",
            str(tmp_path / "does-not-exist.yml"),
            "--fail-on-warn",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "value,expect",
    [
        ("main", True),  # scalar string: invalid per schema, common typo
        (["main"], True),
        (5, False),  # non-iterable: must not raise
        (True, False),
        ([None], False),
        ([["main"]], False),  # nested list: stringified, no crash
    ],
)
def test_branch_filter_normalisation_never_raises(value, expect):
    """A scalar `branches: main` used to iterate characters and return False —
    the opposite of intent — and `branches: 5` raised TypeError. Both are
    invalid workflow files, but a checker should not crash on one."""
    assert ci._push_fires_on_main({"push": {"branches": value}}) is expect


def test_conditional_array_append_does_not_satisfy_the_class_level_test(tmp_path):
    """The class-level guard must not credit a flag that only applies sometimes.

    Adversarial review moved --fail-on-warn from the unconditional INV_ARGS= line
    into a guarded `INV_ARGS+=(...)` and the earlier effective_args() treated it as
    unconditional, letting the mutation through.
    """
    fake = tmp_path / "gate.sh"
    fake.write_text(
        'INV_ARGS=(--format "$FORMAT")\n'
        '[ "$STRICT" = 1 ] && INV_ARGS+=(--fail-on-warn)\n'
        'python3 scripts/check_workflow_invariants.py "${INV_ARGS[@]}"\n',
        encoding="utf-8",
    )
    lines = fake.read_text(encoding="utf-8").splitlines()

    def effective_args(line: str) -> str:
        text = line
        for var in re.findall(r"\$\{(\w+)\[@\]\}", line):
            for candidate in lines:
                stripped = candidate.strip()
                if not stripped.startswith((f"{var}=", f"{var}+=")):
                    continue
                if candidate != stripped:
                    continue
                if "&&" in candidate.split("=", 1)[0] or candidate.lstrip().startswith(
                    "["
                ):
                    continue
                text += " " + candidate
        return text

    invocation = next(
        effective_args(line)
        for line in lines
        if "check_workflow_invariants.py" in line and "python3" in line
    )
    assert "--fail-on-warn" not in invocation, (
        "a conditionally-appended flag must not count as unconditionally present"
    )


def test_gate_runs_yamllint_in_strict_mode():
    """yamllint exits 0 on warning-level findings, and run() discards the output
    of anything exiting 0 — the same muted-findings shape as the two blocking
    bugs, just inside a third-party tool's own pass contract. --strict makes
    warnings count."""
    yamllint_lines = [line for line in GATE.splitlines() if "YAMLLINT_ARGS=(" in line]
    assert yamllint_lines, "expected the gate to build YAMLLINT_ARGS"
    assert any("--strict" in line for line in yamllint_lines), (
        "yamllint must run --strict or warning-level findings exit 0 and are "
        f"discarded. Found: {yamllint_lines}"
    )
