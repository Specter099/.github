#!/usr/bin/env python3
"""
check_workflow_invariants.py

Structural checks for GitHub Actions workflows and composite actions that
off-the-shelf linters don't cover. yamllint checks formatting, actionlint
checks syntax and expression validity, zizmor checks generic security
patterns — this script checks the conventions specific to this org.

Each check has an ID (WF0xx), a severity, and a one-line rationale. Findings
whose fingerprint appears in the baseline file are reported as ACCEPTED and
don't affect the exit code, so known-outstanding work doesn't block commits
while new violations still fail.

Usage:
    python scripts/check_workflow_invariants.py                    # this repo
    python scripts/check_workflow_invariants.py --path ../other    # a caller repo
    python scripts/check_workflow_invariants.py --strict           # ignore baseline
    python scripts/check_workflow_invariants.py --list-checks
    python scripts/check_workflow_invariants.py --format=github    # CI annotations

Exit codes:
    0 — no unaccepted findings at or above the fail threshold
    1 — one or more unaccepted findings
    2 — could not run (unparseable YAML, missing path)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from fnmatch import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Conventions this script enforces. Keep the rationale text short — it is
# printed verbatim next to every finding.
# ---------------------------------------------------------------------------

ORG = "Specter099"
INTERNAL_PREFIX = f"{ORG}/.github"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The protected branch every trigger rule here is written about.
MAIN = "main"
# Steps whose presence marks a workflow as running *checks* rather than deploying.
CHECK_COMMAND_RE = re.compile(
    r"\b(ruff|pytest|bandit|pip-audit|checkov|cfn-lint|yamllint|eslint|vitest)\b"
)
# Expression contexts an attacker can influence via a PR.
UNTRUSTED_CONTEXT_RE = re.compile(
    r"\$\{\{\s*(inputs\.|github\.event\.|github\.head_ref|github\.ref_name)"
)

CHECKS: dict[str, tuple[str, str]] = {
    # id:      (severity, rationale)
    "WF001": ("error", "every job must set timeout-minutes (default is 6 hours)"),
    "WF002": ("error", "actions/checkout must set persist-credentials: false"),
    "WF003": ("error", "third-party actions must be pinned to a 40-char commit SHA"),
    "WF004": ("warn", "internal actions/workflows must be pinned, not @main"),
    "WF005": ("error", "workflow_call must declare every secret it references"),
    "WF006": ("error", "pull_request_target is not permitted in this org"),
    "WF007": ("warn", "GITHUB_OUTPUT heredoc must use a randomised delimiter"),
    "WF008": ("warn", "PR-check workflows must define a concurrency group"),
    "WF009": ("error", "no ${{ }} interpolation of untrusted context inside run:"),
    "WF010": ("error", "every job must declare permissions"),
    "WF011": ("warn", "declared inputs should be referenced somewhere in the file"),
    "WF012": ("error", "README usage examples must only pass declared inputs"),
    "WF013": ("error", "deploy workflows must not run checks (checks belong in CI)"),
    "WF014": (
        "error",
        "a workflow must not trigger on both pull_request and push:main",
    ),
}

FAIL_SEVERITIES = {"error"}


class BaselineError(Exception):
    """The baseline file exists but could not be understood."""


@dataclass
class Finding:
    check: str
    path: str
    line: int
    detail: str
    accepted: bool = False

    @property
    def severity(self) -> str:
        return CHECKS[self.check][0]

    @property
    def rationale(self) -> str:
        return CHECKS[self.check][1]

    @property
    def fingerprint(self) -> str:
        """Stable identity for baselining — deliberately excludes the line
        number so that unrelated edits above a finding don't un-baseline it."""
        return f"{self.check}:{self.path}:{self.detail}"


@dataclass
class Source:
    path: Path
    rel: str
    text: str
    doc: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def load(path: Path, root: Path) -> Source:
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level is not a mapping")
    return Source(path=path, rel=str(path.relative_to(root)), text=text, doc=doc)


def triggers(doc: dict) -> dict:
    """Return the `on:` block.

    PyYAML follows YAML 1.1, where a bare `on:` key is parsed as the boolean
    True. Workflows in this repo use both `on:` and quoted `"on":`, so both
    spellings have to be handled.
    """
    for key in (True, "on", "On", "ON"):
        if key in doc:
            value = doc[key]
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                return {value: None}
            if isinstance(value, list):
                return dict.fromkeys(value)
    return {}


def jobs(doc: dict) -> dict:
    got = doc.get("jobs")
    return got if isinstance(got, dict) else {}


def steps_of(job: dict) -> list[dict]:
    got = job.get("steps")
    return [s for s in got if isinstance(s, dict)] if isinstance(got, list) else []


def action_steps(doc: dict) -> list[tuple[str, dict]]:
    """All steps with a `uses:`, from a workflow or a composite action, as
    (job_name_or_'runs', step) pairs."""
    out: list[tuple[str, dict]] = []
    for name, job in jobs(doc).items():
        if isinstance(job, dict):
            out += [(str(name), s) for s in steps_of(job)]
    runs = doc.get("runs")
    if isinstance(runs, dict):
        out += [("runs", s) for s in steps_of(runs)]
    return out


def line_of(text: str, *needles: str, default: int = 1) -> int:
    """First 1-indexed line containing every needle. Keeps the data model
    clean — PyYAML discards positions, and re-finding them is cheap."""
    for i, line in enumerate(text.splitlines(), 1):
        if all(n in line for n in needles):
            return i
    return default


def is_reusable(doc: dict) -> bool:
    return "workflow_call" in triggers(doc)


def is_composite(doc: dict) -> bool:
    runs = doc.get("runs")
    return isinstance(runs, dict) and runs.get("using") == "composite"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_jobs(src: Source) -> list[Finding]:
    """WF001 timeout-minutes, WF010 permissions."""
    out = []
    for name, job in jobs(src.doc).items():
        if not isinstance(job, dict):
            continue
        # A job that only calls a reusable workflow can't set timeout-minutes
        # or its own permissions — those live in the callee.
        if "uses" in job:
            continue
        ln = line_of(src.text, f"{name}:")
        if "timeout-minutes" not in job:
            out.append(Finding("WF001", src.rel, ln, f"job '{name}'"))
        if "permissions" not in job and "permissions" not in src.doc:
            out.append(Finding("WF010", src.rel, ln, f"job '{name}'"))
    return out


def check_checkout(src: Source) -> list[Finding]:
    """WF002 — a checkout that leaves the token on disk lets any later step
    (including a dependency's build hook) push with it."""
    out = []
    for _job, step in action_steps(src.doc):
        uses = str(step.get("uses", ""))
        if not uses.startswith("actions/checkout@"):
            continue
        with_ = step.get("with") or {}
        if str(with_.get("persist-credentials", "")).lower() != "false":
            out.append(
                Finding(
                    "WF002",
                    src.rel,
                    line_of(src.text, uses),
                    f"uses {uses.split('@')[0]}",
                )
            )
    return out


def _pin_finding(src: Source, uses: str, context: str) -> Finding | None:
    """Classify a single `uses:` value, or None if it needs no pin."""
    uses = uses.strip()
    if not uses or uses.startswith("./") or uses.startswith("docker://"):
        return None
    if "@" not in uses:
        return Finding("WF003", src.rel, line_of(src.text, uses), f"{uses}{context}")
    repo, ref = uses.rsplit("@", 1)
    if SHA_RE.match(ref):
        return None
    check = "WF004" if repo.startswith(INTERNAL_PREFIX) else "WF003"
    return Finding(check, src.rel, line_of(src.text, uses), f"{repo}@{ref}{context}")


def check_pinning(src: Source) -> list[Finding]:
    """WF003 third-party pinned by SHA, WF004 internal pinned too."""
    out = []
    for _job, step in action_steps(src.doc):
        f = _pin_finding(src, str(step.get("uses", "")), "")
        if f:
            out.append(f)

    # Job-level `uses:` — a reusable-workflow call. This is the highest-privilege
    # composition in Actions, because it is the one that can carry
    # `secrets: inherit`, yet it lives on the job rather than in `steps:` and so
    # was previously invisible to the step walk above. An unpinned third-party
    # reusable workflow here is strictly worse than an unpinned step action.
    for name, job in jobs(src.doc).items():
        if not isinstance(job, dict) or "uses" not in job:
            continue
        f = _pin_finding(
            src, str(job["uses"]), f" (reusable workflow call in job '{name}')"
        )
        if f:
            out.append(f)

    # A checkout of another repo without an explicit ref is the same mutable
    # dependency as `uses: ...@main`, and easier to miss.
    for _job, step in action_steps(src.doc):
        uses = str(step.get("uses", ""))
        if not uses.startswith("actions/checkout@"):
            continue
        with_ = step.get("with") or {}
        repo = str(with_.get("repository", ""))
        if repo and not with_.get("ref"):
            out.append(
                Finding(
                    "WF004",
                    src.rel,
                    line_of(src.text, f"repository: {repo}"),
                    f"checkout of {repo} with no ref (implicit default branch)",
                )
            )
    return out


def check_secrets_declared(src: Source) -> list[Finding]:
    """WF005 — an undeclared secret is only populated via `secrets: inherit`,
    which hands the callee every secret the caller holds."""
    if not is_reusable(src.doc):
        return []
    call = triggers(src.doc).get("workflow_call") or {}
    declared = set((call.get("secrets") or {}).keys())
    referenced = {
        m
        for m in re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", src.text)
        if m != "GITHUB_TOKEN"  # always injected, never declarable
    }
    return [
        Finding(
            "WF005",
            src.rel,
            line_of(src.text, f"secrets.{name}"),
            f"references secrets.{name} but does not declare it",
        )
        for name in sorted(referenced - declared)
    ]


def _push_fires_on_main(trig: dict) -> bool:
    """Would this `on:` block run the workflow on a push to main?

    Every spelling below means yes, and each was a real miss at some point:
      push: {branches: [main]}      explicit
      push:                         no filter at all → every branch
      on: [pull_request, push]      list form; triggers() maps push to None
      push: {paths: [...]}          a mapping with no branch filter → every branch
      push: {branches: ["m*"]}      a glob that matches main

    And these mean no:
      push: {branches: [release/**]}   an explicit list that excludes main
      push: {tags: ["v*"]}            tags-only never fires on a branch push, so
                                      there is no merge-time double-run. This is
                                      the common "PR checks + tag release" layout
                                      and flagging it was a false positive.
      push: {branches-ignore: [main]}  main explicitly excluded
    """
    if "push" not in trig:
        return False
    push = trig["push"]
    if not isinstance(push, dict):
        # `push:` (None) or a bare string/list form — no filtering at all.
        return True

    ignore = push.get("branches-ignore")
    if ignore and _matches(MAIN, ignore):
        return False

    branches = push.get("branches")
    if branches is None:
        # No branch filter. A tags-only trigger never fires on a branch push;
        # anything else (paths, no filter at all) fires on every branch.
        tag_only = any(k in push for k in ("tags", "tags-ignore"))
        return not tag_only
    return _matches(MAIN, branches)


def _matches(name: str, patterns) -> bool:
    """Does `name` survive a GitHub branch/tag filter list?

    Entries are globs and may be negated with a leading `!`; later entries
    override earlier ones, so `["**", "!main"]` means "every branch except
    main". Evaluating in order is what distinguishes that from `["**"]`.
    """
    included = False
    for raw in patterns:
        pat = str(raw)
        if pat.startswith("!"):
            if fnmatch(name, pat[1:]):
                included = False
        elif fnmatch(name, pat):
            included = True
    return included


def check_triggers(src: Source) -> list[Finding]:
    """WF006 pull_request_target, WF014 pull_request + push:main together."""
    out = []
    trig = triggers(src.doc)
    if "pull_request_target" in trig:
        out.append(
            Finding(
                "WF006",
                src.rel,
                line_of(src.text, "pull_request_target"),
                "pull_request_target grants a write token to fork-authored code",
            )
        )
    if "pull_request" in trig and _push_fires_on_main(trig):
        out.append(
            Finding(
                "WF014",
                src.rel,
                line_of(src.text, "push:"),
                "triggers on pull_request and push:main — checks double-run at merge",
            )
        )
    return out


def check_output_heredoc(src: Source) -> list[Finding]:
    """WF007 — a fixed heredoc delimiter on command output lets that command's
    text terminate the block early and forge later step outputs."""
    out = []
    for m in re.finditer(
        r'^\s*echo "([A-Za-z0-9_-]+)<<([A-Za-z0-9_]+)"', src.text, re.M
    ):
        name, delim = m.group(1), m.group(2)
        if delim in {"EOF", "END", "DELIM", "EOT"}:
            out.append(
                Finding(
                    "WF007",
                    src.rel,
                    src.text[: m.start()].count("\n") + 1,
                    f"output '{name}' uses fixed heredoc delimiter '{delim}'",
                )
            )
    return out


def check_concurrency(src: Source) -> list[Finding]:
    """WF008 — without cancel-in-progress, every push to a PR runs a full
    duplicate pipeline to completion."""
    name = Path(src.rel).name
    is_check = "review" in name or name in {"python-ci.yml", "self-test.yml"}
    if not is_check:
        return []
    if "concurrency" in src.doc:
        return []
    missing = [
        n
        for n, job in jobs(src.doc).items()
        if isinstance(job, dict) and "concurrency" not in job
    ]
    return [
        Finding("WF008", src.rel, line_of(src.text, f"{n}:"), f"job '{n}'")
        for n in missing
    ]


def check_run_interpolation(src: Source) -> list[Finding]:
    """WF009 — the script-injection check. Untrusted context interpolated into
    a run: body is substituted before the shell parses it."""
    out = []
    for job_name, step in action_steps(src.doc):
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for m in UNTRUSTED_CONTEXT_RE.finditer(run):
            expr = run[m.start() : run.find("}}", m.start()) + 2]
            out.append(
                Finding(
                    "WF009",
                    src.rel,
                    line_of(src.text, expr.strip()),
                    f"job '{job_name}' interpolates {expr.strip()} into run:",
                )
            )
    return out


def check_unused_inputs(src: Source) -> list[Finding]:
    """WF011 — a declared-but-unreferenced input is either dead or a typo at
    the point of use."""
    if is_reusable(src.doc):
        spec = (triggers(src.doc).get("workflow_call") or {}).get("inputs") or {}
    elif is_composite(src.doc):
        spec = src.doc.get("inputs") or {}
    else:
        return []
    out = []
    for name in spec:
        # inputs.foo-bar, inputs['foo-bar'], and INPUT_FOO_BAR (composite env)
        env_name = "INPUT_" + str(name).upper().replace("-", "_")
        patterns = (
            f"inputs.{name}",
            f"inputs['{name}']",
            f'inputs["{name}"]',
            env_name,
        )
        if not any(p in src.text for p in patterns):
            out.append(
                Finding(
                    "WF011",
                    src.rel,
                    line_of(src.text, f"{name}:"),
                    f"input '{name}' is declared but never referenced",
                )
            )
    return out


def check_no_checks_in_deploy(src: Source) -> list[Finding]:
    """WF013 — encodes the repo's 'all checks in CI, CD only deploys' model."""
    if "deploy" not in Path(src.rel).name:
        return []
    out = []
    for job_name, step in action_steps(src.doc):
        run = step.get("run")
        blob = f"{step.get('name', '')} {run if isinstance(run, str) else ''}"
        m = CHECK_COMMAND_RE.search(blob)
        # `npm run build` is a deploy-time artifact build, not a check.
        if m and "run build" not in blob:
            out.append(
                Finding(
                    "WF013",
                    src.rel,
                    line_of(src.text, m.group(0)),
                    f"job '{job_name}' runs '{m.group(0)}' in a deploy workflow",
                )
            )
    return out


def check_readme_examples(root: Path, workflows: dict[str, Source]) -> list[Finding]:
    """WF012 — every input in a README usage example must exist. GitHub rejects
    unknown inputs to a reusable workflow, so a stale example is a broken
    copy-paste for whoever follows the docs.
    """
    readme = root / "README.md"
    if not readme.is_file():
        return []
    text = readme.read_text(encoding="utf-8")
    out = []
    # Each `uses: <org>/.github/.github/workflows/<name>.yml@ref` followed by an
    # optional `with:` block whose keys should all be declared inputs.
    pattern = re.compile(
        r"uses:\s*\S+/\.github/workflows/(?P<wf>[\w.-]+\.yml)@\S+"
        r"(?P<rest>(?:\n[ \t]+\S.*)*)",
    )
    for m in pattern.finditer(text):
        wf, rest = m.group("wf"), m.group("rest")
        src = workflows.get(wf)
        if src is None:
            out.append(
                Finding(
                    "WF012",
                    "README.md",
                    text[: m.start()].count("\n") + 1,
                    f"documents {wf}, which does not exist",
                )
            )
            continue
        declared = set(
            ((triggers(src.doc).get("workflow_call") or {}).get("inputs") or {}).keys()
        )
        if "with:" not in rest:
            continue
        after_with = rest.split("with:", 1)[1]
        for km in re.finditer(r"^\s+([a-zA-Z][\w-]*):", after_with, re.M):
            key = km.group(1)
            if key not in declared:
                out.append(
                    Finding(
                        "WF012",
                        "README.md",
                        line_of(text, f"{key}:", default=1),
                        f"{wf} example passes '{key}', which is not a declared input",
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

PER_FILE_CHECKS = (
    check_jobs,
    check_checkout,
    check_pinning,
    check_secrets_declared,
    check_triggers,
    check_output_heredoc,
    check_concurrency,
    check_run_interpolation,
    check_unused_inputs,
    check_no_checks_in_deploy,
)


def collect(root: Path) -> tuple[list[Finding], list[str]]:
    """Run every check. Returns (findings, hard_errors)."""
    findings: list[Finding] = []
    errors: list[str] = []
    workflows: dict[str, Source] = {}

    paths = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    paths += sorted((root / ".github" / "actions").glob("*/action.y*ml"))
    if not paths:
        errors.append(f"no workflows or actions found under {root}/.github")
        return findings, errors

    for path in paths:
        try:
            src = load(path, root)
        except (yaml.YAMLError, ValueError) as exc:
            errors.append(f"{path.relative_to(root)}: {exc}")
            continue
        if "workflows" in path.parts:
            workflows[path.name] = src
        for check in PER_FILE_CHECKS:
            findings += check(src)

    findings += check_readme_examples(root, workflows)
    return findings, errors


def load_baseline(path: Path) -> tuple[dict[str, int], dict]:
    """Return {fingerprint: accepted_count}.

    Counts, not a bare set: two genuinely distinct violations in the same file
    can share a fingerprint (same check, same path, same detail text — e.g. a
    second `echo "x<<EOF"` added to a workflow that already has a baselined
    one). With a set, the new one was silently absorbed by the old entry. An
    entry accepts `count` findings (default 1); anything beyond that is new and
    blocks.
    """
    if not path.is_file():
        return {}, {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            raise ValueError("top level is not a mapping")
        counts: dict[str, int] = {}
        for a in doc.get("accepted") or []:
            if not isinstance(a, dict) or "fingerprint" not in a:
                continue
            fp = str(a["fingerprint"])
            n = int(a.get("count", 1))
            if n < 1:
                raise ValueError(f"count must be >= 1, got {n!r} for {fp}")
            counts[fp] = counts.get(fp, 0) + n
        return counts, doc
    except (yaml.YAMLError, ValueError, TypeError, AttributeError) as exc:
        # Fail closed and legibly. Silently ignoring a broken baseline would
        # accept nothing and bury the reason in a wall of findings; a traceback
        # is correct-but-unreadable. Exit 2 is the documented "could not run".
        raise BaselineError(f"{path}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--path", default=".", help="repo root to check (default: .)")
    parser.add_argument(
        "--baseline",
        default=None,
        help="baseline YAML (default: <path>/.github/workflow-invariants-baseline.yml)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="ignore the baseline — every finding counts",
    )
    parser.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="treat warn-severity findings as failures too",
    )
    parser.add_argument(
        "--format",
        choices=("text", "github", "json"),
        default="text",
        help="github emits ::error/::warning annotations",
    )
    parser.add_argument("--list-checks", action="store_true", help="list check IDs")
    args = parser.parse_args()

    if args.list_checks:
        for cid, (sev, why) in CHECKS.items():
            print(f"{cid}  {sev:<5}  {why}")
        return 0

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: --path not found: {root}", file=sys.stderr)
        return 2

    findings, errors = collect(root)
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    if errors:
        return 2

    baseline_path = (
        Path(args.baseline)
        if args.baseline
        else root / ".github" / "workflow-invariants-baseline.yml"
    )
    try:
        accepted, _ = ({}, {}) if args.strict else load_baseline(baseline_path)
    except BaselineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Consume the per-fingerprint budget in order; findings past it are new.
    remaining = dict(accepted)
    for f in findings:
        budget = remaining.get(f.fingerprint, 0)
        if budget > 0:
            f.accepted = True
            remaining[f.fingerprint] = budget - 1

    fail_sev = FAIL_SEVERITIES | ({"warn"} if args.fail_on_warn else set())
    blocking = [f for f in findings if not f.accepted and f.severity in fail_sev]
    advisory = [f for f in findings if not f.accepted and f.severity not in fail_sev]
    tolerated = [f for f in findings if f.accepted]

    if args.format == "json":
        print(
            json.dumps(
                {
                    "blocking": [vars(f) | {"severity": f.severity} for f in blocking],
                    "advisory": [vars(f) | {"severity": f.severity} for f in advisory],
                    "accepted": len(tolerated),
                },
                indent=2,
            )
        )
        return 1 if blocking else 0

    def emit(f: Finding, kind: str) -> None:
        if args.format == "github":
            level = "error" if kind == "blocking" else "warning"
            print(
                f"::{level} file={f.path},line={f.line},title={f.check}::"
                f"{f.detail} — {f.rationale}"
            )
        else:
            mark = {"blocking": "✗", "advisory": "!", "accepted": "·"}[kind]
            print(f"  {mark} {f.check} {f.path}:{f.line}  {f.detail}")

    if blocking:
        print(f"\n{len(blocking)} blocking finding(s):")
        for f in sorted(blocking, key=lambda f: (f.check, f.path, f.line)):
            emit(f, "blocking")
            print(f"      → {f.rationale}")
    if advisory:
        print(f"\n{len(advisory)} advisory finding(s) (warn severity):")
        for f in sorted(advisory, key=lambda f: (f.check, f.path, f.line)):
            emit(f, "advisory")
    if tolerated and args.format == "text":
        print(
            f"\n{len(tolerated)} accepted via baseline (use --strict to show as new):"
        )
        by_check: dict[str, int] = {}
        for f in tolerated:
            by_check[f.check] = by_check.get(f.check, 0) + 1
        for cid in sorted(by_check):
            print(f"  · {cid} ×{by_check[cid]}  {CHECKS[cid][1]}")

    if not blocking:
        print(f"\nOK — no blocking findings ({len(findings)} total, checked {root}).")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
