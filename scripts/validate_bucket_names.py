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

Usage:
    python scripts/validate_bucket_names.py [--path <dir>]

Exit codes:
    0 — all bucket names conform (or none found)
    1 — one or more violations found
"""

import argparse
import ast
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
# ---------------------------------------------------------------------------
BUCKET_NAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9-]+-\d{12}-[a-z]{2}-[a-z]+-\d-an$"
)


def is_valid_bucket_name(name: str) -> bool:
    return bool(BUCKET_NAME_RE.match(name))


def extract_bucket_names_from_file(path: Path) -> list[tuple[int, str]]:
    """
    Parse a Python file with the AST and extract string literals passed as
    the `bucket_name` keyword argument to any function/constructor call.

    Returns a list of (line_number, bucket_name_value) tuples.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        print(f"  Warning: Could not parse {path}: {exc}", file=sys.stderr)
        return []

    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "bucket_name" and isinstance(kw.value, ast.Constant):
                results.append((kw.value.lineno, str(kw.value.value)))

    return results


def scan_directory(root: Path) -> tuple[list[tuple[Path, int, str]], int]:
    """
    Recursively scan all .py files under root for bucket_name= arguments.

    Returns a tuple of (violations, checked_count).
    """
    violations = []
    checked = 0

    for py_file in sorted(root.rglob("*.py")):
        parts = py_file.parts
        if any(p in parts for p in ("cdk.out", ".venv", "venv", "node_modules", "__pycache__")):
            continue

        names = extract_bucket_names_from_file(py_file)
        for lineno, name in names:
            checked += 1
            if not is_valid_bucket_name(name):
                violations.append((py_file, lineno, name))

    return violations, checked


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate S3 bucket names in CDK Python source.")
    parser.add_argument(
        "--path",
        default=".",
        help="Root directory to scan (default: current directory)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress passing output; only print violations",
    )
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Error: Path not found or not a directory: {root}", file=sys.stderr)
        return 1

    print(f"Scanning {root} for S3 bucket_name= arguments...")
    violations, checked = scan_directory(root)

    print(f"   Found {checked} hardcoded bucket name(s) across all .py files.\n")

    if not violations:
        if not args.quiet:
            print("All bucket names conform to the naming convention.")
            print("   Pattern: {prefix}-{12-digit-account-id}-{aws-region}-an")
        return 0

    print(f"{len(violations)} bucket name violation(s) found:\n")
    print("   Required pattern: {prefix}-{12-digit-account-id}-{aws-region}-an")
    print("   Example:          bitwarden-logs-123456789012-us-east-1-an\n")

    for file_path, lineno, name in violations:
        rel = file_path.relative_to(root) if file_path.is_relative_to(root) else file_path
        print(f"   {rel}:{lineno}")
        print(f'     bucket_name = "{name}"')
        print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
