from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from paper_html import RecordError, render_record, validate_html_file  # noqa: E402


class PaperHtmlPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir_obj.name)
        (self.root / "papers" / "sample-2026").mkdir(parents=True)
        self.pdf_path = self.root / "papers" / "sample-2026" / "paper.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with self.pdf_path.open("wb") as stream:
            writer.write(stream)
        self.pdf_sha256 = hashlib.sha256(self.pdf_path.read_bytes()).hexdigest()
        self.code_path = self.root / "code" / "sample-2026"
        self.code_path.mkdir(parents=True)
        (self.code_path / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.code_path / "LICENSE").write_text("MIT fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.code_path), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.code_path), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.code_path),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            check=True,
        )
        self.code_commit = subprocess.run(
            ["git", "-C", str(self.code_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        self.output = self.root / "site" / "sample-2026.html"
        self.output.parent.mkdir()

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    def record(self) -> dict[str, object]:
        return {
            "paper_id": "sample-2026",
            "title": "A <Safe> Paper & Its Method",
            "title_zh": None,
            "authors": ["Author A"],
            "year": 2026,
            "venue": "TestConf",
            "doi": None,
            "arxiv_id": None,
            "summary": "已核验摘要。",
            "abstract": None,
            "tags": ["fixture"],
            "domain_slugs": ["test-domain"],
            "source_urls": [{"label": "项目页", "url": "https://example.org/paper"}],
            "pdf": {
                "path": "../papers/sample-2026/paper.pdf",
                "sha256": self.pdf_sha256,
                "size": self.pdf_path.stat().st_size,
                "page_count": 1,
                "version": "fixture-v1",
            },
            "analysis": {
                "problem": None,
                "method": ["步骤一"],
                "results": None,
                "limitations": ["限制一"],
                "relationships": None,
                "reproduction": ["固定输入"],
            },
            "code": {
                "status": "official_local",
                "url": "https://github.com/example/sample",
                "local_path": "../code/sample-2026/",
                "commit": self.code_commit,
                "license": "MIT",
                "evidence": ["项目页链接仓库"],
            },
            "decision_evidence": ["fixture"],
            "warnings": [],
        }

    def render(self, record: dict[str, object] | None = None) -> str:
        rendered, _ = render_record(
            record or self.record(),
            template_path=TOOL_DIR / "template.html",
            styles_path=TOOL_DIR / "styles.css",
        )
        self.output.write_text(rendered, encoding="utf-8", newline="\n")
        return rendered

    def test_render_is_deterministic_safe_and_explicit_about_unknowns(self) -> None:
        first = self.render()
        second, _ = render_record(
            self.record(),
            template_path=TOOL_DIR / "template.html",
            styles_path=TOOL_DIR / "styles.css",
        )
        self.assertEqual(first, second)
        self.assertIn("A &lt;Safe&gt; Paper &amp; Its Method", first)
        self.assertNotIn("A <Safe> Paper", first)
        self.assertIn("待核验", first)
        self.assertNotIn("<script", first.lower())

    def test_validator_accepts_generated_page_and_checks_links(self) -> None:
        self.render()
        result = validate_html_file(self.output, allowed_root=self.root)
        self.assertTrue(result.passed, [issue.message for issue in result.errors])
        self.assertGreaterEqual(result.links_checked, 8)
        self.assertEqual({asset["kind"] for asset in result.verified_assets}, {"pdf", "code"})
        self.assertTrue(any(issue.code == "metadata_pending" for issue in result.warnings))

    def test_validator_detects_pdf_fingerprint_and_git_commit_mismatch(self) -> None:
        rendered = self.render()
        cases = [
            (self.pdf_sha256, "0" * 64, "pdf_sha256_mismatch"),
            (self.code_commit, "0" * len(self.code_commit), "git_commit_mismatch"),
        ]
        for old, new, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                self.output.write_text(rendered.replace(old, new), encoding="utf-8")
                result = validate_html_file(self.output, allowed_root=self.root)
                self.assertFalse(result.passed)
                self.assertTrue(any(issue.code == expected_code for issue in result.errors))

    def test_validator_reports_missing_local_link(self) -> None:
        record = self.record()
        record["pdf"] = {"path": "../papers/missing/paper.pdf"}
        self.render(record)
        result = validate_html_file(self.output)
        self.assertFalse(result.passed)
        self.assertTrue(any(issue.code == "local_link_missing" for issue in result.errors))

    def test_validator_rejects_link_outside_allowed_root(self) -> None:
        outside = self.root.parent / "outside-paper.pdf"
        outside.write_bytes(self.pdf_path.read_bytes())
        try:
            record = self.record()
            record["pdf"] = {"path": "../../outside-paper.pdf"}
            self.render(record)
            result = validate_html_file(self.output, allowed_root=self.root)
            self.assertFalse(result.passed)
            self.assertTrue(
                any(issue.code == "local_link_outside_allowed_root" for issue in result.errors)
            )
        finally:
            outside.unlink(missing_ok=True)

    def test_renderer_rejects_unsafe_url(self) -> None:
        record = self.record()
        record["source_urls"] = [{"label": "bad", "url": "javascript:alert(1)"}]
        with self.assertRaises(RecordError):
            render_record(
                record,
                template_path=TOOL_DIR / "template.html",
                styles_path=TOOL_DIR / "styles.css",
            )

    def test_renderer_rejects_remote_local_representation_paths(self) -> None:
        for container, field in (("pdf", "path"), ("code", "local_path")):
            with self.subTest(container=container, field=field):
                record = self.record()
                nested = dict(record[container])  # type: ignore[arg-type]
                nested[field] = "https://evil.example/fake"
                record[container] = nested
                with self.assertRaises(RecordError):
                    render_record(
                        record,
                        template_path=TOOL_DIR / "template.html",
                        styles_path=TOOL_DIR / "styles.css",
                    )

    def test_runtime_schema_semantics_reject_missing_source_url(self) -> None:
        record = self.record()
        record["source_urls"] = [{}]
        with self.assertRaises(RecordError):
            render_record(
                record,
                template_path=TOOL_DIR / "template.html",
                styles_path=TOOL_DIR / "styles.css",
            )

    def test_validator_rejects_inline_script(self) -> None:
        rendered = self.render()
        self.output.write_text(
            rendered.replace("</body>", "<script>alert(1)</script></body>"),
            encoding="utf-8",
        )
        result = validate_html_file(self.output)
        self.assertFalse(result.passed)
        self.assertTrue(any(issue.code == "prohibited_elements" for issue in result.errors))

    def test_validator_rejects_remote_resource_variants_and_css_escapes(self) -> None:
        rendered = self.render()
        variants = [
            '<img alt="x" srcset="https://cdn.example/x.png 1x">',
            '<video poster="//cdn.example/poster.jpg"></video>',
            '<link rel="icon" href="https://cdn.example/favicon.ico">',
            '<svg><use href="https://cdn.example/sprite.svg#x"></use></svg>',
            r'<div style="background-image:url(\68 ttps://cdn.example/x.png)"></div>',
            '<style>@import "https://cdn.example/theme.css";</style>',
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.output.write_text(
                    rendered.replace("</body>", variant + "</body>"), encoding="utf-8"
                )
                result = validate_html_file(self.output, allowed_root=self.root)
                self.assertFalse(result.passed)
                self.assertTrue(
                    any(
                        issue.code in {"remote_subresource", "css_import_forbidden"}
                        or issue.code == "subresource_url_invalid"
                        for issue in result.errors
                    ),
                    [issue.code for issue in result.errors],
                )

    def test_validator_rejects_duplicate_attributes_using_browser_first_value(self) -> None:
        rendered = self.render()
        injected = '<a href="javascript:alert(1)" href="#main">bad</a>'
        self.output.write_text(rendered.replace("</body>", injected + "</body>"), encoding="utf-8")
        result = validate_html_file(self.output, allowed_root=self.root)
        self.assertFalse(result.passed)
        self.assertTrue(any(issue.code == "duplicate_attributes" for issue in result.errors))
        self.assertTrue(any(issue.code == "unsafe_link" for issue in result.errors))

    def test_bad_later_record_does_not_partially_write_batch(self) -> None:
        good = self.record()
        bad = self.record()
        bad["paper_id"] = "bad/path"
        input_path = self.root / "batch.json"
        input_path.write_text(json.dumps([good, bad], ensure_ascii=False), encoding="utf-8")
        output_dir = self.root / "batch-output"
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_DIR / "generate.py"),
                "--input",
                str(input_path),
                "--output",
                str(output_dir),
                "--allowed-root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse((output_dir / "sample-2026.html").exists())

    def test_validation_failure_leaves_no_new_page_and_preserves_overwrite_target(self) -> None:
        record = self.record()
        record["pdf"] = {"path": "../papers/missing/paper.pdf"}
        input_path = self.root / "missing-record.json"
        input_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        target = self.root / "site" / "transaction.html"
        command = [
            sys.executable,
            str(TOOL_DIR / "generate.py"),
            "--input",
            str(input_path),
            "--output",
            str(target),
            "--allowed-root",
            str(self.root),
        ]
        first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 1, first.stderr + first.stdout)
        self.assertFalse(target.exists())
        target.write_text("ORIGINAL", encoding="utf-8")
        second = subprocess.run(
            [*command, "--overwrite"], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(second.returncode, 1, second.stderr + second.stdout)
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL")

    def test_encoded_parent_escape_is_rejected_by_required_root(self) -> None:
        outside = self.root.parent / "encoded-outside.pdf"
        outside.write_bytes(self.pdf_path.read_bytes())
        try:
            record = self.record()
            record["pdf"] = {
                "path": "%2e%2e/%2e%2e/encoded-outside.pdf",
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                "size": outside.stat().st_size,
                "page_count": 1,
                "version": "fixture",
            }
            input_path = self.root / "encoded-record.json"
            input_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            target = self.root / "site" / "encoded.html"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_DIR / "generate.py"),
                    "--input",
                    str(input_path),
                    "--output",
                    str(target),
                    "--allowed-root",
                    str(self.root),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 1, completed.stderr + completed.stdout)
            self.assertFalse(target.exists())
        finally:
            outside.unlink(missing_ok=True)

    def test_cli_generates_and_validates(self) -> None:
        input_path = self.root / "record.json"
        input_path.write_text(json.dumps(self.record(), ensure_ascii=False), encoding="utf-8")
        report_path = self.root / "report.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_DIR / "generate.py"),
                "--input",
                str(input_path),
                "--output",
                str(self.output),
                "--allowed-root",
                str(self.root),
                "--report",
                str(report_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["generated"], 1)

    def test_cli_rejects_report_output_collision_without_traceback(self) -> None:
        input_path = self.root / "record.json"
        input_path.write_text(json.dumps(self.record(), ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_DIR / "generate.py"),
                "--input",
                str(input_path),
                "--output",
                str(self.output),
                "--allowed-root",
                str(self.root),
                "--report",
                str(self.output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_catches_bad_template_placeholder_without_traceback(self) -> None:
        input_path = self.root / "record.json"
        input_path.write_text(json.dumps(self.record(), ensure_ascii=False), encoding="utf-8")
        bad_template = self.root / "bad-template.html"
        bad_template.write_text("$unknown_placeholder", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_DIR / "generate.py"),
                "--input",
                str(input_path),
                "--output",
                str(self.output),
                "--allowed-root",
                str(self.root),
                "--template",
                str(bad_template),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
