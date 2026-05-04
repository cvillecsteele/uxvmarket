from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from uxv_mirroring.contracts import MirrorCorpus, MirrorPolicy, MirrorTarget, QualityReport, TargetCoverageEntry, TargetRegistry
from uxv_mirroring.registry import (
    find_covered_entry,
    load_registry,
    normalize_homepage_url,
    policy_hash,
    save_registry,
    update_registry_for_corpus,
)
from uxv_mirroring.materialize import write_json


class RegistryTests(unittest.TestCase):
    def test_policy_hash_is_stable_and_homepage_normalizes(self) -> None:
        self.assertEqual(policy_hash(MirrorPolicy()), policy_hash(MirrorPolicy.model_validate(MirrorPolicy().model_dump())))
        self.assertEqual(normalize_homepage_url("HTTPS://Example.COM/"), "https://example.com")

    def test_complete_entry_matches_but_partial_does_not(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        policy = MirrorPolicy()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            manifest = workspace / "corpus.json"
            corpus = MirrorCorpus(
                target=target,
                policy=policy,
                run_id="run-1",
                corpus_root=str(workspace),
                manifest_path=str(manifest),
                crawl_index_path=str(workspace / "crawl.json"),
                quality_report_path=str(workspace / "quality.json"),
                resources=[],
                quality_report=QualityReport(
                    status="complete",
                    fetched_pages=1,
                    failed_pages=0,
                    fetched_documents=0,
                    discovered_urls=1,
                    skipped_urls=0,
                    total_text_chars=500,
                ),
            )
            write_json(manifest, corpus.model_dump())
            update_registry_for_corpus(workspace, corpus)
            registry = load_registry(workspace)
            self.assertIsNotNone(find_covered_entry(registry, target=target, policy=policy))

            registry.entries[0].quality_status = "partial"
            self.assertIsNone(find_covered_entry(registry, target=target, policy=policy))

    def test_stale_and_missing_manifest_entries_do_not_match(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        policy = MirrorPolicy()
        entry = TargetCoverageEntry(
            target_id="example",
            display_name="Example",
            homepage_url="https://example.com",
            normalized_homepage_url="https://example.com",
            profile="quick_evidence",
            policy_hash=policy_hash(policy),
            quality_status="complete",
            run_id="run-1",
            corpus_manifest_path="/tmp/does-not-exist.json",
            quality_report_path="/tmp/quality.json",
            resource_count=1,
            updated_at="2000-01-01T00:00:00+00:00",
        )
        registry = TargetRegistry(entries=[entry])
        self.assertIsNone(find_covered_entry(registry, target=target, policy=policy))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text("not valid corpus", encoding="utf-8")
            entry.corpus_manifest_path = str(manifest)
            self.assertIsNone(find_covered_entry(registry, target=target, policy=policy, max_age_days=1))

    def test_save_registry_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            save_registry(workspace, TargetRegistry())
            self.assertEqual(load_registry(workspace).entries, [])


if __name__ == "__main__":
    unittest.main()

