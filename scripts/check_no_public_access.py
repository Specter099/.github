#!/usr/bin/env python3
"""
Check CloudFormation templates for public access using AWS IAM Access Analyzer.

Scans *.template.json files in the given directory, extracts resource policies
from supported resource types, and calls CheckNoPublicAccess for each.

Exit codes:
  0 — all resources pass (or no applicable resources found)
  1 — one or more resources grant public access (when --fail-on-public-access)
"""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Maps CloudFormation resource type → (policy property, Access Analyzer resource type)
POLICY_MAP = {
    "AWS::S3::BucketPolicy": ("PolicyDocument", "AWS::S3::Bucket"),
    "AWS::SQS::QueuePolicy": ("PolicyDocument", "AWS::SQS::Queue"),
    "AWS::SNS::TopicPolicy": ("PolicyDocument", "AWS::SNS::Topic"),
    "AWS::KMS::Key": ("KeyPolicy", "AWS::KMS::Key"),
    "AWS::ECR::Repository": ("RepositoryPolicyText", "AWS::ECR::Repository"),
    "AWS::SecretsManager::ResourcePolicy": (
        "ResourcePolicy",
        "AWS::SecretsManager::Secret",
    ),
    "AWS::IAM::Role": (
        "AssumeRolePolicyDocument",
        "AWS::IAM::AssumeRolePolicyDocument",
    ),
}

# CDK metadata files to skip
SKIP_FILES = {"manifest.json", "tree.json", "cdk.out"}


def find_templates(template_dir: Path) -> list[Path]:
    templates = []
    for path in sorted(template_dir.rglob("*.template.json")):
        if path.name not in SKIP_FILES and not path.name.startswith("asset."):
            templates.append(path)
    return templates


def extract_policies(template: dict) -> list[tuple[str, str, dict]]:
    """Return list of (logical_id, analyzer_resource_type, policy_document)."""
    results = []
    resources = template.get("Resources", {})
    for logical_id, resource in resources.items():
        cf_type = resource.get("Type", "")
        if cf_type not in POLICY_MAP:
            continue
        policy_prop, analyzer_type = POLICY_MAP[cf_type]
        policy_doc = resource.get("Properties", {}).get(policy_prop)
        if policy_doc is None:
            continue
        results.append((logical_id, analyzer_type, policy_doc))
    return results


def check_policy(client, logical_id: str, analyzer_type: str, policy_doc: dict) -> dict:
    """Call CheckNoPublicAccess and return a result dict."""
    try:
        resp = client.check_no_public_access(
            policyDocument=json.dumps(policy_doc),
            resourceType=analyzer_type,
        )
        is_public = resp.get("result") == "FAIL"
        reasons = [r.get("description", "") for r in resp.get("reasons", [])]
        return {
            "logical_id": logical_id,
            "resource_type": analyzer_type,
            "public": is_public,
            "reasons": reasons,
        }
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        return {
            "logical_id": logical_id,
            "resource_type": analyzer_type,
            "public": False,
            "error": f"{code}: {msg}",
        }


def write_summary(findings: list[dict], template_name: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a") as f:
        f.write(f"\n### Access Analyzer — `{template_name}`\n\n")
        if not findings:
            f.write("_No applicable resources found._\n")
            return
        f.write("| Resource | Type | Result |\n")
        f.write("|---|---|---|\n")
        for r in findings:
            if "error" in r:
                icon = "⚠️"
                status = f"Error: {r['error']}"
            elif r["public"]:
                icon = "❌"
                reasons = (
                    "; ".join(r["reasons"])
                    if r["reasons"]
                    else "public access detected"
                )
                status = f"FAIL — {reasons}"
            else:
                icon = "✅"
                status = "PASS"
            f.write(
                f"| `{r['logical_id']}` | `{r['resource_type']}` | {icon} {status} |\n"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template-dir",
        default="cdk.out",
        help="Directory containing CloudFormation *.template.json files (default: cdk.out)",
    )
    parser.add_argument(
        "--aws-region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region for Access Analyzer API calls",
    )
    parser.add_argument(
        "--fail-on-public-access",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exit 1 if public access is detected (default: true)",
    )
    args = parser.parse_args()

    template_dir = Path(args.template_dir)
    if not template_dir.is_dir():
        print(
            f"::error::template-dir '{template_dir}' does not exist or is not a directory"
        )
        return 1

    templates = find_templates(template_dir)
    if not templates:
        print(
            f"::notice::No *.template.json files found in '{template_dir}' — skipping check"
        )
        return 0

    client = boto3.client("accessanalyzer", region_name=args.aws_region)

    total_violations = 0

    for template_path in templates:
        template_name = template_path.name
        print(f"\n=== {template_name} ===")

        try:
            template = json.loads(template_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"::warning::Could not parse {template_name}: {exc}")
            continue

        policies = extract_policies(template)
        if not policies:
            print("  No applicable resources found — skipping")
            write_summary([], template_name)
            continue

        findings = []
        for logical_id, analyzer_type, policy_doc in policies:
            result = check_policy(client, logical_id, analyzer_type, policy_doc)
            findings.append(result)

            if "error" in result:
                print(f"  ⚠  {logical_id} ({analyzer_type}) — error: {result['error']}")
            elif result["public"]:
                total_violations += 1
                reasons = (
                    "; ".join(result["reasons"])
                    if result["reasons"]
                    else "public access detected"
                )
                print(f"  ✗  {logical_id} ({analyzer_type}) — FAIL: {reasons}")
            else:
                print(f"  ✓  {logical_id} ({analyzer_type}) — PASS")

        write_summary(findings, template_name)

    print()
    if total_violations > 0:
        print(f"::error::{total_violations} resource(s) grant public access")
        return 1 if args.fail_on_public_access else 0

    print("All resources passed the no-public-access check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
