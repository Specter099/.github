"""Tests for scripts/validate_bucket_names.py."""

import json
import textwrap

import validate_bucket_names as vbn


# ---------------------------------------------------------------------------
# is_valid_bucket_name — convention: {prefix}-{12-digit-account}-{region}-an
# ---------------------------------------------------------------------------
class TestIsValidBucketName:
    def test_valid_examples_from_docstring(self):
        assert vbn.is_valid_bucket_name("bitwarden-logs-123456789012-us-east-1-an")
        assert vbn.is_valid_bucket_name("cloudfront-access-123456789012-eu-west-2-an")

    def test_single_token_prefix(self):
        assert vbn.is_valid_bucket_name("logs-123456789012-ap-southeast-2-an")

    def test_missing_suffix(self):
        assert not vbn.is_valid_bucket_name("bitwarden-123456789012-us-east-1")

    def test_account_id_not_12_digits(self):
        assert not vbn.is_valid_bucket_name("bitwarden-12345-us-east-1-an")

    def test_underscore_in_prefix(self):
        assert not vbn.is_valid_bucket_name("bitwarden_logs-123456789012-us-east-1-an")

    def test_uppercase_rejected(self):
        assert not vbn.is_valid_bucket_name("Bitwarden-123456789012-us-east-1-an")

    def test_plain_name_rejected(self):
        assert not vbn.is_valid_bucket_name("my-bucket")


# ---------------------------------------------------------------------------
# extract_bucket_names_from_file — AST scan of bucket_name= kwargs
# ---------------------------------------------------------------------------
class TestExtractBucketNames:
    def test_extracts_keyword_argument(self, tmp_path):
        src = tmp_path / "stack.py"
        src.write_text(
            textwrap.dedent(
                """
                s3.Bucket(
                    self,
                    "Logs",
                    bucket_name="logs-123456789012-us-east-1-an",
                )
                """
            )
        )
        found = vbn.extract_bucket_names_from_file(src)
        assert [(name, valid) for _, name, valid in found] == [
            ("logs-123456789012-us-east-1-an", True)
        ]

    def test_invalid_literal_marked(self, tmp_path):
        src = tmp_path / "stack.py"
        # Built indirectly so this test file itself never contains a
        # non-conforming bucket_name= literal.
        src.write_text('s3.Bucket(self, "B", bucket_name=%s)\n' % '"my-bucket"')
        found = vbn.extract_bucket_names_from_file(src)
        assert [(name, valid) for _, name, valid in found] == [("my-bucket", False)]

    def test_ignores_other_kwargs(self, tmp_path):
        src = tmp_path / "stack.py"
        src.write_text('s3.Bucket(self, "B", versioned=True)\n')
        assert vbn.extract_bucket_names_from_file(src) == []

    def test_syntax_error_raises(self, tmp_path):
        import pytest

        src = tmp_path / "broken.py"
        src.write_text("def (:\n")  # invalid syntax
        with pytest.raises(SyntaxError):
            vbn.extract_bucket_names_from_file(src)

    def test_conforming_fstring_passes(self, tmp_path):
        src = tmp_path / "stack.py"
        src.write_text('B(bucket_name=f"logs-{account_id}-{region}-an")\n')
        found = vbn.extract_bucket_names_from_file(src)
        assert len(found) == 1
        _, display, valid = found[0]
        assert valid
        assert display == "logs-{account_id}-{region}-an"

    def test_fstring_missing_suffix_flagged(self, tmp_path):
        src = tmp_path / "stack.py"
        src.write_text('B(bucket_name=f"logs-{account_id}-{region}")\n')
        found = vbn.extract_bucket_names_from_file(src)
        assert [valid for _, _, valid in found] == [False]

    def test_fstring_bad_literal_chars_flagged(self, tmp_path):
        src = tmp_path / "stack.py"
        src.write_text('B(bucket_name=f"My_Logs-{account_id}-{region}-an")\n')
        found = vbn.extract_bucket_names_from_file(src)
        assert [valid for _, _, valid in found] == [False]


# ---------------------------------------------------------------------------
# scan_directory
# ---------------------------------------------------------------------------
class TestScanDirectory:
    def test_flags_invalid_names(self, tmp_path):
        (tmp_path / "good.py").write_text(
            'B(bucket_name="logs-123456789012-us-east-1-an")\n'
        )
        (tmp_path / "bad.py").write_text('B(bucket_name="my-bucket")\n')
        violations, checked, parse_errors = vbn.scan_directory(tmp_path)
        assert checked == 2
        assert len(violations) == 1
        assert violations[0][2] == "my-bucket"
        assert parse_errors == 0

    def test_excludes_vendored_dirs(self, tmp_path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "dep.py").write_text('B(bucket_name="my-bucket")\n')
        violations, checked, parse_errors = vbn.scan_directory(tmp_path)
        assert checked == 0
        assert violations == []
        assert parse_errors == 0

    def test_unparseable_file_counted(self, tmp_path):
        (tmp_path / "broken.py").write_text("def (:\n")
        violations, checked, parse_errors = vbn.scan_directory(tmp_path)
        assert violations == []
        assert checked == 0
        assert parse_errors == 1


# ---------------------------------------------------------------------------
# scan_templates — synthesized CloudFormation
# ---------------------------------------------------------------------------
class TestScanTemplates:
    def _write_template(self, path, bucket_name):
        props = {} if bucket_name is None else {"BucketName": bucket_name}
        path.write_text(
            json.dumps(
                {
                    "Resources": {
                        "Bucket": {"Type": "AWS::S3::Bucket", "Properties": props}
                    }
                }
            )
        )

    def test_flags_invalid_template_name(self, tmp_path):
        self._write_template(tmp_path / "Stack.template.json", "my-bucket")
        violations, checked, parse_errors = vbn.scan_templates(tmp_path)
        assert checked == 1
        assert len(violations) == 1

    def test_valid_template_name_passes(self, tmp_path):
        self._write_template(
            tmp_path / "Stack.template.json", "logs-123456789012-us-east-1-an"
        )
        violations, checked, parse_errors = vbn.scan_templates(tmp_path)
        assert checked == 1
        assert violations == []

    def test_autogenerated_name_skipped(self, tmp_path):
        # No BucketName property -> auto-named, not checked.
        self._write_template(tmp_path / "Stack.template.json", None)
        violations, checked, parse_errors = vbn.scan_templates(tmp_path)
        assert checked == 0

    def test_intrinsic_bucketname_skipped(self, tmp_path):
        # Non-string BucketName (intrinsic) is skipped.
        (tmp_path / "Stack.template.json").write_text(
            json.dumps(
                {
                    "Resources": {
                        "Bucket": {
                            "Type": "AWS::S3::Bucket",
                            "Properties": {"BucketName": {"Ref": "Param"}},
                        }
                    }
                }
            )
        )
        violations, checked, parse_errors = vbn.scan_templates(tmp_path)
        assert checked == 0

    def test_manifest_and_asset_skipped(self, tmp_path):
        self._write_template(tmp_path / "manifest.json", "my-bucket")
        self._write_template(tmp_path / "asset.abc.template.json", "my-bucket")
        violations, checked, parse_errors = vbn.scan_templates(tmp_path)
        assert checked == 0

    def test_malformed_template_counted(self, tmp_path):
        (tmp_path / "Stack.template.json").write_text("{not json")
        violations, checked, parse_errors = vbn.scan_templates(tmp_path)
        assert violations == []
        assert checked == 0
        assert parse_errors == 1


# ---------------------------------------------------------------------------
# main — exit codes
# ---------------------------------------------------------------------------
class TestMain:
    def test_exit_zero_when_all_valid(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "good.py").write_text(
            'B(bucket_name="logs-123456789012-us-east-1-an")\n'
        )
        monkeypatch.setattr("sys.argv", ["prog", "--path", str(tmp_path)])
        assert vbn.main() == 0

    def test_exit_one_on_violation(self, tmp_path, monkeypatch):
        (tmp_path / "bad.py").write_text('B(bucket_name="my-bucket")\n')
        monkeypatch.setattr("sys.argv", ["prog", "--path", str(tmp_path)])
        assert vbn.main() == 1

    def test_requires_an_argument(self, monkeypatch):
        import pytest

        monkeypatch.setattr("sys.argv", ["prog"])
        with pytest.raises(SystemExit):
            vbn.main()

    def test_exit_two_on_unparseable_file(self, tmp_path, monkeypatch):
        (tmp_path / "broken.py").write_text("def (:\n")
        monkeypatch.setattr("sys.argv", ["prog", "--path", str(tmp_path)])
        assert vbn.main() == 2

    def test_violation_takes_precedence_over_parse_error(self, tmp_path, monkeypatch):
        (tmp_path / "broken.py").write_text("def (:\n")
        (tmp_path / "bad.py").write_text("B(bucket_name=%s)\n" % '"my-bucket"')
        monkeypatch.setattr("sys.argv", ["prog", "--path", str(tmp_path)])
        assert vbn.main() == 1
