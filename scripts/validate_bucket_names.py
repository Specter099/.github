#!/usr/bin/env python3
"""
validate_bucket_names.py

Scans CDK Python source files for S3 bucket_name= arguments and validates
they conform to the required naming convention:

    {prefix}-{12-digit-account-id}-{aws-region}-an

Examples of VALID names:
    bitwarden-logs-123456789012-us-east-1-an
    cloudfront-access-123456789012-eu-west-2-an

Examples of INVALID names:
    my-bucket                          (missing account/region/suffix)
    bitwarden-123456789012-us-east-1   (missing -an suffix)
    bitwarden-12345-us-east-1-an       (account ID not 12 digits)
    bitwarden_logs-123456789012-us-east-1-an  (underscore in prefix)

f-strings (e.g. bucket_name=f"logs-{account}-{region}-an") are validated
best-effort: interpolated expressions can't be resolved statically, so only
the literal parts are checked (kebab-case characters, literal "-an" suffix).

Usage:
    python scripts/validate_bucket_names.py [--path <dir>]

Exit codes:
    0 — all bucket names conform (or none found)
    1 — one or more violations found
    2 — scan incomplete: a Python file or template could not be parsed
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Naming convention regex
# ---------------------------------------------------------------------------
# Segments:
#   prefix      : one or more lowercase alphanumeric/hyphen tokens (kebab-case)
#   account-id  : exactly 12 digits
#   region      : standard AWS region format  e.g. us-east-1, ap-southeast-2
#   suffix      : literal "an"
# The region segment is intentionally loose ({2 letters}-{word}-{digit})
# to avoid maintaining a hard-coded list of AWS regions.
# ---------------------------------------------------------------------------
BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]+-\d{12}-[a-z]{2}-[a-z]+-\d-an$")


def is_valid_bucket_name(name: str) -> bool:
    return bool(BUCKET_NAME_RE.match(name))


def _render_fstring(node: ast.JoinedStr) -> str:
    """Render an f-string for display: literal parts verbatim, interpolated
    expressions as {expr} placeholders."""
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
        elif isinstance(value, ast.FormattedValue):
            parts.append("{" + ast.unparse(value.value) + "}")
    return "".join(parts)


def _fstring_conforms(node: ast.JoinedStr) -> bool:
    """
    Best-effort validation of an f-string bucket name. Interpolated values
    can't be resolved statically, so check what is checkable:
      - every literal part uses only kebab-case characters [a-z0-9-]
      - the name ends with a literal "-an" suffix
    """
    last = node.values[-1] if node.values else None
    if not (isinstance(last, ast.Constant) and str(last.value).endswith("-an")):
        return False
    return all(
        re.fullmatch(r"[a-z0-9-]*", str(value.value))
        for value in node.values
        if isinstance(value, ast.Constant)
    )


def extract_bucket_names_from_file(path: Path) -> list[tuple[int, str, bool]]:
    """
    Parse a Python file with the AST and extract values passed as the
    `bucket_name` keyword argument to any function/constructor call.

    Handles string literals (validated against the full convention) and
    f-strings (validated best-effort on their literal parts).

    Returns a list of (line_number, display_value, is_valid) tuples.
    Raises SyntaxError if the file cannot be parsed.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "bucket_name":
                continue
            if isinstance(kw.value, ast.Constant):
                name = str(kw.value.value)
                results.append((kw.value.lineno, name, is_valid_bucket_name(name)))
            elif isinstance(kw.value, ast.JoinedStr):
                display = _render_fstring(kw.value)
                results.append((kw.value.lineno, display, _fstring_conforms(kw.value)))

    return results


def scan_directory(root: Path) -> tuple[list[tuple[Path, int, str]], int, int]:
    """
    Recursively scan all .py files under root for bucket_name= arguments.

    Returns a tuple of (violations, checked_count, parse_errors).
    Files that fail to parse are counted in parse_errors rather than
    silently skipped, so callers can fail loudly on an incomplete scan.
    """
    violations = []
    checked = 0
    parse_errors = 0

    for py_file in sorted(root.rglob("*.py")):
        parts = py_file.parts
        if any(
            p in parts
            for p in ("cdk.out", ".venv", "venv", "node_modules", "__pycache__")
        ):
            continue

        try:
            names = extract_bucket_names_from_file(py_file)
        except SyntaxError as exc:
            print(f"  Warning: Could not parse {py_file}: {exc}", file=sys.stderr)
            parse_errors += 1
            continue
        for lineno, name, valid in names:
            checked += 1
            if not valid:
                violations.append((py_file, lineno, name))

    return violations, checked, parse_errors


def scan_templates(template_dir: Path) -> tuple[list[tuple[Path, str, str]], int, int]:
    """
    Scan CloudFormation *.template.json files for AWS::S3::Bucket resources
    with a literal-string BucketName property and validate each name.

    Returns (violations, checked_count, parse_errors).
    Each violation is (template_path, logical_id, bucket_name). Templates
    that fail to parse are counted in parse_errors rather than silently
    skipped, so callers can fail loudly on an incomplete scan.
    """
    skip = {"manifest.json", "tree.json"}
    violations = []
    checked = 0
    parse_errors = 0

    for tpl_path in sorted(template_dir.rglob("*.template.json")):
        if tpl_path.name in skip or tpl_path.name.startswith("asset."):
            continue
        try:
            template = json.loads(tpl_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  Warning: Could not parse {tpl_path.name}: {exc}", file=sys.stderr)
            parse_errors += 1
            continue

        for logical_id, resource in template.get("Resources", {}).items():
            if resource.get("Type") != "AWS::S3::Bucket":
                continue
            bucket_name = resource.get("Properties", {}).get("BucketName")
            if bucket_name is None or not isinstance(bucket_name, str):
                # No explicit name (auto-generated) or intrinsic function — skip
                continue
            checked += 1
            if not is_valid_bucket_name(bucket_name):
                violations.append((tpl_path, logical_id, bucket_name))

    return violations, checked, parse_errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate S3 bucket names in CDK source or synthesized templates."
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Root directory to scan for bucket_name= in Python source files",
    )
    parser.add_argument(
        "--template-dir",
        default=None,
        help="Directory containing CloudFormation *.template.json files (e.g. cdk.out)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress passing output; only print violations",
    )
    args = parser.parse_args()

    if not args.path and not args.template_dir:
        parser.error(
            "Provide --path (Python source scan) or --template-dir (CF template scan), or both."
        )

    total_violations = 0
    total_parse_errors = 0

    if args.template_dir:
        tdir = Path(args.template_dir).resolve()
        if not tdir.is_dir():
            print(f"Error: --template-dir not found: {tdir}", file=sys.stderr)
            return 1
        print(f"Scanning CloudFormation templates in {tdir} ...")
        tpl_violations, tpl_checked, tpl_parse_errors = scan_templates(tdir)
        total_parse_errors += tpl_parse_errors
        if tpl_violations:
            total_violations += len(tpl_violations)
            print(f"\n{len(tpl_violations)} bucket name violation(s) in templates:\n")
            print("   Required pattern: {prefix}-{12-digit-account-id}-{aws-region}-an")
            print("   Example:          bitwarden-logs-123456789012-us-east-1-an\n")
            for tpl_path, logical_id, name in tpl_violations:
                print(f"   {tpl_path.name} / {logical_id}")
                print(f'     BucketName = "{name}"')
                print()
        elif not args.quiet:
            print(f"   Checked {tpl_checked} explicit bucket name(s) — all conform.")

    if args.path:
        root = Path(args.path).resolve()
        if not root.is_dir():
            print(f"Error: --path not found: {root}", file=sys.stderr)
            return 1
        print(f"\nScanning {root} for S3 bucket_name= arguments...")
        src_violations, src_checked, src_parse_errors = scan_directory(root)
        total_parse_errors += src_parse_errors
        print(
            f"   Found {src_checked} hardcoded bucket name(s) across all .py files.\n"
        )
        if src_violations:
            total_violations += len(src_violations)
            print(f"{len(src_violations)} bucket name violation(s) found:\n")
            print("   Required pattern: {prefix}-{12-digit-account-id}-{aws-region}-an")
            print("   Example:          bitwarden-logs-123456789012-us-east-1-an\n")
            for file_path, lineno, name in src_violations:
                rel = (
                    file_path.relative_to(root)
                    if file_path.is_relative_to(root)
                    else file_path
                )
                print(f"   {rel}:{lineno}")
                print(f'     bucket_name = "{name}"')
                print()
        elif not args.quiet:
            print("All bucket names conform to the naming convention.")
            print("   Pattern: {prefix}-{12-digit-account-id}-{aws-region}-an")

    if total_violations > 0:
        return 1
    if total_parse_errors > 0:
        print(
            f"Error: {total_parse_errors} file(s) could not be parsed — "
            "scan incomplete.",
            file=sys.stderr,
        )
        return 2
    if not args.quiet:
        print("All checked bucket names pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
