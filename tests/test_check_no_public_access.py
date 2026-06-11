"""Tests for scripts/check_no_public_access.py."""

import json

import pytest
from botocore.exceptions import ClientError

import check_no_public_access as cnpa


# ---------------------------------------------------------------------------
# find_templates
# ---------------------------------------------------------------------------
class TestFindTemplates:
    def test_skips_metadata_and_assets(self, tmp_path):
        (tmp_path / "Stack.template.json").write_text("{}")
        (tmp_path / "manifest.json").write_text("{}")
        (tmp_path / "tree.json").write_text("{}")
        (tmp_path / "asset.deadbeef.template.json").write_text("{}")
        names = [p.name for p in cnpa.find_templates(tmp_path)]
        assert names == ["Stack.template.json"]


# ---------------------------------------------------------------------------
# extract_policies
# ---------------------------------------------------------------------------
class TestExtractPolicies:
    def test_extracts_bucket_policy(self):
        template = {
            "Resources": {
                "BP": {
                    "Type": "AWS::S3::BucketPolicy",
                    "Properties": {"PolicyDocument": {"Version": "2012-10-17"}},
                }
            }
        }
        policies, errors = cnpa.extract_policies(template)
        assert policies == [("BP", "AWS::S3::Bucket", {"Version": "2012-10-17"})]
        assert errors == []

    def test_stringified_policy_parsed(self):
        # CloudFormation allows policy documents serialized as JSON strings.
        template = {
            "Resources": {
                "BP": {
                    "Type": "AWS::S3::BucketPolicy",
                    "Properties": {
                        "PolicyDocument": json.dumps({"Version": "2012-10-17"})
                    },
                }
            }
        }
        policies, errors = cnpa.extract_policies(template)
        assert policies == [("BP", "AWS::S3::Bucket", {"Version": "2012-10-17"})]
        assert errors == []

    def test_unparseable_policy_string_reported(self):
        template = {
            "Resources": {
                "BP": {
                    "Type": "AWS::S3::BucketPolicy",
                    "Properties": {"PolicyDocument": "{not json"},
                }
            }
        }
        policies, errors = cnpa.extract_policies(template)
        assert policies == []
        assert len(errors) == 1
        assert errors[0][0] == "BP"

    def test_iam_role_excluded(self):
        # IAM::Role trust policies are intentionally not analyzed.
        template = {
            "Resources": {
                "Role": {
                    "Type": "AWS::IAM::Role",
                    "Properties": {"AssumeRolePolicyDocument": {"x": 1}},
                }
            }
        }
        assert cnpa.extract_policies(template) == ([], [])
        assert "AWS::IAM::Role" not in cnpa.POLICY_MAP

    def test_resource_without_policy_skipped(self):
        template = {
            "Resources": {"BP": {"Type": "AWS::S3::BucketPolicy", "Properties": {}}}
        }
        assert cnpa.extract_policies(template) == ([], [])

    def test_unknown_type_skipped(self):
        template = {"Resources": {"Fn": {"Type": "AWS::Lambda::Function"}}}
        assert cnpa.extract_policies(template) == ([], [])


# ---------------------------------------------------------------------------
# check_policy
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self, result="PASS", reasons=None, raises=None):
        self._result = result
        self._reasons = reasons or []
        self._raises = raises

    def check_no_public_access(self, policyDocument, resourceType):
        if self._raises:
            raise self._raises
        return {"result": self._result, "reasons": self._reasons}


class TestCheckPolicy:
    def test_pass(self):
        r = cnpa.check_policy(FakeClient("PASS"), "R", "AWS::S3::Bucket", {})
        assert r["public"] is False
        assert "error" not in r

    def test_fail_with_reasons(self):
        client = FakeClient("FAIL", reasons=[{"description": "anyone can read"}])
        r = cnpa.check_policy(client, "R", "AWS::S3::Bucket", {})
        assert r["public"] is True
        assert r["reasons"] == ["anyone can read"]

    def test_client_error_captured(self):
        err = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad policy"}},
            "CheckNoPublicAccess",
        )
        r = cnpa.check_policy(FakeClient(raises=err), "R", "AWS::S3::Bucket", {})
        assert r["public"] is False
        assert "ValidationException" in r["error"]


# ---------------------------------------------------------------------------
# write_summary
# ---------------------------------------------------------------------------
class TestWriteSummary:
    def test_writes_table(self, tmp_path, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        cnpa.write_summary(
            [
                {
                    "logical_id": "R",
                    "resource_type": "AWS::S3::Bucket",
                    "public": False,
                    "reasons": [],
                }
            ],
            "Stack.template.json",
        )
        text = summary.read_text()
        assert "Stack.template.json" in text
        assert "PASS" in text

    def test_noop_without_env(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        # Should not raise.
        cnpa.write_summary([], "Stack.template.json")


# ---------------------------------------------------------------------------
# main — exit codes (boto3 client stubbed)
# ---------------------------------------------------------------------------
class TestMain:
    def _template(self, tmp_path):
        (tmp_path / "Stack.template.json").write_text(
            json.dumps(
                {
                    "Resources": {
                        "BP": {
                            "Type": "AWS::S3::BucketPolicy",
                            "Properties": {"PolicyDocument": {"Version": "2012-10-17"}},
                        }
                    }
                }
            )
        )

    def test_no_templates_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.argv", ["prog", "--template-dir", str(tmp_path)])
        assert cnpa.main() == 0

    def test_public_access_exits_one(self, tmp_path, monkeypatch):
        self._template(tmp_path)
        monkeypatch.setattr(cnpa.boto3, "client", lambda *a, **k: FakeClient("FAIL"))
        monkeypatch.setattr("sys.argv", ["prog", "--template-dir", str(tmp_path)])
        assert cnpa.main() == 1

    def test_public_access_warn_only_exits_zero(self, tmp_path, monkeypatch):
        self._template(tmp_path)
        monkeypatch.setattr(cnpa.boto3, "client", lambda *a, **k: FakeClient("FAIL"))
        monkeypatch.setattr(
            "sys.argv",
            ["prog", "--template-dir", str(tmp_path), "--no-fail-on-public-access"],
        )
        assert cnpa.main() == 0

    def test_all_pass_exits_zero(self, tmp_path, monkeypatch):
        self._template(tmp_path)
        monkeypatch.setattr(cnpa.boto3, "client", lambda *a, **k: FakeClient("PASS"))
        monkeypatch.setattr("sys.argv", ["prog", "--template-dir", str(tmp_path)])
        assert cnpa.main() == 0

    def test_missing_dir_exits_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["prog", "--template-dir", str(tmp_path / "nope")]
        )
        assert cnpa.main() == 1

    def test_api_error_exits_two(self, tmp_path, monkeypatch):
        self._template(tmp_path)
        err = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no perms"}},
            "CheckNoPublicAccess",
        )
        monkeypatch.setattr(
            cnpa.boto3, "client", lambda *a, **k: FakeClient(raises=err)
        )
        monkeypatch.setattr("sys.argv", ["prog", "--template-dir", str(tmp_path)])
        assert cnpa.main() == 2

    def test_malformed_template_exits_two(self, tmp_path, monkeypatch):
        (tmp_path / "Stack.template.json").write_text("{not json")
        monkeypatch.setattr(cnpa.boto3, "client", lambda *a, **k: FakeClient("PASS"))
        monkeypatch.setattr("sys.argv", ["prog", "--template-dir", str(tmp_path)])
        assert cnpa.main() == 2

    def test_violation_takes_precedence_over_error(self, tmp_path, monkeypatch):
        # One template fails to parse, another has a public policy → exit 1.
        (tmp_path / "Broken.template.json").write_text("{not json")
        self._template(tmp_path)
        monkeypatch.setattr(cnpa.boto3, "client", lambda *a, **k: FakeClient("FAIL"))
        monkeypatch.setattr("sys.argv", ["prog", "--template-dir", str(tmp_path)])
        assert cnpa.main() == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
