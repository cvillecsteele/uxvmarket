from __future__ import annotations

import unittest

from pydantic import ValidationError

from uxv_mirroring.contracts import CrawlIndexEntry, MirrorPolicy, MirrorResource, MirrorTarget, QualityReport, RunState, TargetRunState


class ContractTests(unittest.TestCase):
    def test_contracts_round_trip_and_reject_unknown_fields(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        policy = MirrorPolicy(profile="quick_evidence", max_browserless_calls_per_target=10)
        resource = MirrorResource(
            resource_id="resource-0001",
            url="https://example.com",
            kind="html",
            status="fetched",
            json_path="/tmp/page.json",
        )
        quality = QualityReport(
            status="complete",
            fetched_pages=1,
            failed_pages=0,
            fetched_documents=0,
            discovered_urls=1,
            skipped_urls=0,
            total_text_chars=500,
        )
        crawl_entry = CrawlIndexEntry(
            url="https://example.com/privacy-policy",
            status="skipped_class_budget",
            page_class="company",
        )

        self.assertEqual(MirrorTarget.model_validate(target.model_dump()).target_id, "example")
        self.assertEqual(MirrorPolicy.model_validate(policy.model_dump()).profile, "quick_evidence")
        self.assertEqual(MirrorPolicy.model_validate(policy.model_dump()).max_browserless_calls_per_target, 10)
        self.assertEqual(MirrorResource.model_validate(resource.model_dump()).kind, "html")
        self.assertEqual(CrawlIndexEntry.model_validate(crawl_entry.model_dump()).page_class, "company")
        self.assertEqual(MirrorPolicy.model_validate(policy.model_dump()).page_class_budgets["news"], 1)
        self.assertEqual(QualityReport.model_validate(quality.model_dump()).status, "complete")
        with self.assertRaises(ValidationError):
            MirrorTarget.model_validate(
                {
                    "target_id": "bad",
                    "display_name": "Bad",
                    "homepage_url": "https://example.com",
                    "extra": True,
                }
            )

    def test_run_state_round_trip_and_rejects_unknown_fields(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        state = RunState(
            run_id="run-1",
            workspace_root="/tmp/work",
            profile="quick_evidence",
            policy=MirrorPolicy(),
            targets=[target],
            target_states=[TargetRunState(target_id="example")],
        )
        self.assertEqual(RunState.model_validate(state.model_dump()).run_id, "run-1")
        with self.assertRaises(ValidationError):
            RunState.model_validate({**state.model_dump(), "extra": True})


if __name__ == "__main__":
    unittest.main()
