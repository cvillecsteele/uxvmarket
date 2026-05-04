from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from uxv_mirroring.contracts import MirrorPolicy, MirrorTarget
from uxv_mirroring.state import (
    initialize_run_state,
    load_run_state,
    mark_target,
    mark_url,
    recover_running_work,
    save_run_state,
    set_selected_urls,
    summarize_run_state,
    target_state_for,
    validate_unique_targets,
)


class RunStateTests(unittest.TestCase):
    def test_initialize_save_load_and_summarize(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            state = initialize_run_state(run_id="run-1", workspace_root=workspace, targets=[target], policy=MirrorPolicy())
            target_state = target_state_for(state, "example")
            set_selected_urls(target_state, ["https://example.com", "https://example.com/products"])
            mark_target(state, "example", "running")
            mark_url(state, "example", "https://example.com", "fetched", resource_id="resource-0001")
            save_run_state(workspace, state)

            loaded = load_run_state(workspace, "run-1")
            summary = summarize_run_state(loaded)
            self.assertEqual(summary["run_id"], "run-1")
            self.assertEqual(summary["url_counts"], {"fetched": 1, "pending": 1})

    def test_recover_running_and_retry_failed(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        state = initialize_run_state(run_id="run-1", workspace_root=Path("/tmp/work"), targets=[target], policy=MirrorPolicy())
        target_state = target_state_for(state, "example")
        set_selected_urls(target_state, ["https://example.com", "https://example.com/broken"])
        mark_target(state, "example", "running")
        mark_url(state, "example", "https://example.com", "running")
        mark_url(state, "example", "https://example.com/broken", "failed", error_message="boom")

        recovered = recover_running_work(state, retry_failed=False)
        recovered_target = target_state_for(recovered, "example")
        statuses = {url_state.url: url_state.status for url_state in recovered_target.urls}
        self.assertEqual(recovered_target.status, "pending")
        self.assertEqual(statuses["https://example.com"], "pending")
        self.assertEqual(statuses["https://example.com/broken"], "failed")

        retried = recover_running_work(recovered, retry_failed=True)
        retry_statuses = {url_state.url: url_state.status for url_state in target_state_for(retried, "example").urls}
        self.assertEqual(retry_statuses["https://example.com/broken"], "pending")

    def test_duplicate_targets_are_rejected(self) -> None:
        targets = [
            MirrorTarget(target_id="example", display_name="One", homepage_url="https://one.example"),
            MirrorTarget(target_id="example", display_name="Two", homepage_url="https://two.example"),
        ]
        with self.assertRaises(ValueError):
            validate_unique_targets(targets)


if __name__ == "__main__":
    unittest.main()

