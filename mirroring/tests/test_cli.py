from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uxv_mirroring.cli import main, parse_target_file
from uxv_mirroring.contracts import MirrorCorpus, MirrorPolicy, MirrorTarget, QualityReport, TargetRegistry
from uxv_mirroring.registry import save_registry
from uxv_mirroring.state import initialize_run_state, save_run_state


class CliTests(unittest.TestCase):
    def test_inspect_run_prints_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output" / "runs" / "abc123" / "manifest.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"run_id":"abc123"}\n', encoding="utf-8")
            with patch("builtins.print") as mocked:
                code = main(["inspect-run", "abc123", "--workspace-root", tmp])
            self.assertEqual(code, 0)
            mocked.assert_called()

    def test_mirror_cli_uses_client(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        quality = QualityReport(
            status="complete",
            fetched_pages=1,
            failed_pages=0,
            fetched_documents=0,
            discovered_urls=1,
            skipped_urls=0,
            total_text_chars=500,
        )
        corpus = MirrorCorpus(
            target=target,
            policy=MirrorPolicy(),
            run_id="cli-run",
            corpus_root="/tmp/corpus",
            manifest_path="/tmp/corpus/manifest.json",
            crawl_index_path="/tmp/corpus/crawl_index.json",
            quality_report_path="/tmp/corpus/quality_report.json",
            resources=[],
            quality_report=quality,
        )
        with patch("uxv_mirroring.cli.MirrorClient") as mocked_client:
            mocked_client.return_value.mirror_targets.return_value = [corpus]
            with patch("builtins.print") as mocked_print:
                code = main(["mirror", "--target", "Example=https://example.com", "--max-calls-per-target", "6"])
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(payload["run_id"], "cli-run")
        kwargs = mocked_client.return_value.mirror_targets.call_args.kwargs
        self.assertEqual(kwargs["coverage_mode"], "reuse")
        self.assertEqual(kwargs["policy"].max_browserless_calls_per_target, 6)

    def test_target_file_parses_jsonl_comments_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.jsonl"
            path.write_text(
                "\n"
                "# comment\n"
                '{"display_name":"Example","homepage_url":"https://example.com","categories":["communications"]}\n',
                encoding="utf-8",
            )
            targets = parse_target_file(path)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_id, "example")
        self.assertEqual(targets[0].categories, ["communications"])

    def test_duplicate_targets_fail_early(self) -> None:
        with patch("uxv_mirroring.cli.MirrorClient") as mocked_client:
            code = main(
                [
                    "mirror",
                    "--target",
                    "Example=https://example.com",
                    "--target",
                    "Example=https://example.org",
                ]
            )
        self.assertEqual(code, 1)
        mocked_client.assert_not_called()

    def test_resume_dispatches_with_resume_true(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        quality = QualityReport(
            status="complete",
            fetched_pages=1,
            failed_pages=0,
            fetched_documents=0,
            discovered_urls=1,
            skipped_urls=0,
            total_text_chars=500,
        )
        corpus = MirrorCorpus(
            target=target,
            policy=MirrorPolicy(),
            run_id="resume-run",
            corpus_root="/tmp/corpus",
            manifest_path="/tmp/corpus/manifest.json",
            crawl_index_path="/tmp/corpus/crawl_index.json",
            quality_report_path="/tmp/corpus/quality_report.json",
            resources=[],
            quality_report=quality,
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            state = initialize_run_state(run_id="resume-run", workspace_root=workspace, targets=[target], policy=MirrorPolicy())
            save_run_state(workspace, state)
            with patch("uxv_mirroring.cli.MirrorClient") as mocked_client:
                mocked_client.return_value.mirror_targets.return_value = [corpus]
                with patch("builtins.print"):
                    code = main(["resume", "resume-run", "--workspace-root", tmp])
        self.assertEqual(code, 0)
        kwargs = mocked_client.return_value.mirror_targets.call_args.kwargs
        self.assertTrue(kwargs["resume"])
        self.assertFalse(kwargs["retry_failed"])

    def test_status_prints_state_summary_and_missing_state_fails(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            state = initialize_run_state(run_id="status-run", workspace_root=workspace, targets=[target], policy=MirrorPolicy())
            save_run_state(workspace, state)
            with patch("builtins.print") as mocked_print:
                code = main(["status", "status-run", "--workspace-root", tmp])
            self.assertEqual(code, 0)
            payload = json.loads(mocked_print.call_args.args[0])
            self.assertEqual(payload["run_id"], "status-run")
            self.assertEqual(main(["status", "missing", "--workspace-root", tmp]), 1)

    def test_coverage_flags_map_to_client_args(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        quality = QualityReport(
            status="complete",
            fetched_pages=1,
            failed_pages=0,
            fetched_documents=0,
            discovered_urls=1,
            skipped_urls=0,
            total_text_chars=500,
        )
        corpus = MirrorCorpus(
            target=target,
            policy=MirrorPolicy(),
            run_id="cli-run",
            corpus_root="/tmp/corpus",
            manifest_path="/tmp/corpus/manifest.json",
            crawl_index_path="/tmp/corpus/crawl_index.json",
            quality_report_path="/tmp/corpus/quality_report.json",
            resources=[],
            quality_report=quality,
        )
        with patch("uxv_mirroring.cli.MirrorClient") as mocked_client:
            mocked_client.return_value.mirror_targets.return_value = [corpus]
            with patch("builtins.print"):
                code = main(["mirror", "--target", "Example=https://example.com", "--skip-covered", "--max-age-days", "7"])
        self.assertEqual(code, 0)
        kwargs = mocked_client.return_value.mirror_targets.call_args.kwargs
        self.assertEqual(kwargs["coverage_mode"], "skip")
        self.assertEqual(kwargs["max_age_days"], 7)

        with patch("uxv_mirroring.cli.MirrorClient") as mocked_client:
            mocked_client.return_value.mirror_targets.return_value = [corpus]
            with patch("builtins.print"):
                code = main(["mirror", "--target", "Example=https://example.com", "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(mocked_client.return_value.mirror_targets.call_args.kwargs["coverage_mode"], "force")

    def test_coverage_command_prints_empty_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_registry(Path(tmp), TargetRegistry())
            with patch("builtins.print") as mocked_print:
                code = main(["coverage", "--workspace-root", tmp])
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(payload["entry_count"], 0)

    def test_coverage_command_checks_specific_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("builtins.print") as mocked_print:
                code = main(["coverage", "--workspace-root", tmp, "--target", "Example=https://example.com"])
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertFalse(payload["covered"])


if __name__ == "__main__":
    unittest.main()
