from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uxv_mirroring.browserless import BrowserlessMapResult, BrowserlessSmartScrapeResult
from uxv_mirroring.contracts import MirrorTarget
from uxv_mirroring.mirror import MirrorClient, classify_page, is_in_scope, policy_for_profile
from uxv_mirroring.registry import load_registry
from uxv_mirroring.state import load_run_state


class FakeStaticResponse:
    def __init__(self, url: str, content: bytes = b"", status_code: int = 200, content_type: str = "text/html") -> None:
        self.url = url
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class FakeStaticClient:
    def __init__(self, responses: dict[str, FakeStaticResponse] | None = None) -> None:
        self.responses = responses or {}
        self.calls = []

    def get(self, url: str):
        self.calls.append(url)
        return self.responses.get(url, FakeStaticResponse(url=url, content=b"<html></html>"))


class FakeBrowserless:
    def __init__(self) -> None:
        self.map_calls = []
        self.scrape_calls = []
        self.scrape_payloads: dict[str, BrowserlessSmartScrapeResult] = {}

    def map_urls(self, *, url, search=None, limit=100, include_subdomains=False, include_sitemaps=True, timeout_ms=60000):
        self.map_calls.append(
            {
                "url": url,
                "search": search,
                "limit": limit,
                "include_subdomains": include_subdomains,
                "include_sitemaps": include_sitemaps,
                "timeout_ms": timeout_ms,
            }
        )
        return BrowserlessMapResult(
            urls=[
                url,
                "https://example.com/products/autopilot",
                "https://example.com/about",
                "https://example.com/catalog.pdf",
                "https://cdn.example.net/logo.png",
                "https://example.com/blog/post",
            ],
            raw={"success": True},
        )

    def smart_scrape(self, *, url, timeout_ms=60000):
        self.scrape_calls.append(url)
        return self.scrape_payloads.get(
            url,
            BrowserlessSmartScrapeResult.model_validate(
                {
                    "ok": True,
                    "statusCode": 200,
                    "content": f"<html><body><h1>{url}</h1><a href='/contact'>Contact</a></body></html>",
                    "contentType": "text/html",
                    "headers": {"content-type": "text/html"},
                    "strategy": "http-fetch",
                    "attempted": ["http-fetch"],
                    "markdown": f"# {url}\n\nRendered vendor evidence for UAS products and manufacturing.",
                    "links": ["https://example.com/contact"],
                }
            ),
        )


class MirrorOrchestrationTests(unittest.TestCase):
    def test_scope_treats_apex_and_www_as_same_site(self) -> None:
        self.assertTrue(is_in_scope("https://allocor.tech/products", "https://www.allocor.tech/", allow_subdomains=False))
        self.assertTrue(is_in_scope("https://www.allocor.tech/products", "https://allocor.tech/", allow_subdomains=False))

    def test_page_classification_covers_legal_news_and_careers(self) -> None:
        policy = policy_for_profile("serious_vendor")
        home = "https://shield.ai"
        self.assertEqual(classify_page("https://shield.ai/privacy-policy", home_url=home, policy=policy), "company")
        self.assertEqual(classify_page("https://shield.ai/terms-of-service", home_url=home, policy=policy), "company")
        self.assertEqual(classify_page("https://shield.ai/careers", home_url=home, policy=policy), "career")
        self.assertEqual(
            classify_page("https://shield.ai/shield-ai-and-l3harris-team-for-breakthrough-in-autonomy", home_url=home, policy=policy),
            "news",
        )
        self.assertEqual(classify_page("https://shield.ai/hivemind-solutions", home_url=home, policy=policy), "product")

    def test_page_class_planner_caps_news_and_careers(self) -> None:
        browserless = FakeBrowserless()

        def map_urls(*, url, search=None, limit=100, include_subdomains=False, include_sitemaps=True, timeout_ms=60000):
            browserless.map_calls.append({"url": url, "search": search})
            return BrowserlessMapResult(
                urls=[
                    "https://shield.ai",
                    "https://shield.ai/hivemind-solutions",
                    "https://shield.ai/v-bat",
                    "https://shield.ai/ai-pilot",
                    "https://shield.ai/about",
                    "https://shield.ai/company-executives",
                    "https://shield.ai/contact",
                    "https://shield.ai/privacy-policy",
                    "https://shield.ai/careers",
                    "https://shield.ai/shield-ai-and-l3harris-team-for-breakthrough-in-autonomy",
                    "https://shield.ai/booz-allen-and-shield-ai-partnership-brings-new-autonomous-airborne-solutions-to-u-s-military",
                    "https://shield.ai/2025-a-new-chapter-for-shield-ai",
                    "https://shield.ai/2024-a-year-of-mission-impact-at-shield-ai",
                ],
                raw={},
            )

        browserless.map_urls = map_urls
        target = MirrorTarget(target_id="shield-ai", display_name="Shield AI", homepage_url="https://shield.ai")
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_profile("serious_vendor")
            policy.max_pages = 12
            policy.max_browserless_calls_per_target = 20
            corpus = MirrorClient(browserless=browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=Path(tmp),
                run_id="class-planner",
                coverage_mode="force",
            )[0]
            fetched = {resource.url for resource in corpus.resources if resource.status == "fetched"}
            self.assertIn("https://shield.ai/hivemind-solutions", fetched)
            self.assertIn("https://shield.ai/privacy-policy", fetched)
            self.assertNotIn("https://shield.ai/careers", fetched)
            fetched_news = [
                entry for entry in corpus.crawl_index
                if entry.page_class == "news" and entry.status == "fetched"
            ]
            self.assertLessEqual(len(fetched_news), policy.max_news_pages)
            skipped_career = [entry for entry in corpus.crawl_index if entry.url == "https://shield.ai/careers"]
            self.assertEqual(skipped_career[0].status, "skipped_class_budget")

    def test_vendor_corpus_writes_html_json_markdown_text_and_reports(self) -> None:
        browserless = FakeBrowserless()
        static = FakeStaticClient(
            {
                "https://example.com": FakeStaticResponse("https://example.com"),
                "https://example.com/catalog.pdf": FakeStaticResponse(
                    "https://example.com/catalog.pdf",
                    content=b"%PDF-1.4\nnot a real pdf but stored raw",
                    content_type="application/pdf",
                ),
            }
        )
        target = MirrorTarget(
            target_id="example",
            display_name="Example",
            homepage_url="https://example.com",
            categories=["flight_vehicle_control"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_profile("quick_evidence")
            corpora = MirrorClient(browserless=browserless, static_client=static).mirror_targets(
                [target],
                policy=policy,
                workspace_root=Path(tmp),
                run_id="test-run",
            )
            corpus = corpora[0]
            self.assertEqual(corpus.run_id, "test-run")
            self.assertGreaterEqual(len(browserless.map_calls), 4)
            self.assertIn("https://example.com", browserless.scrape_calls)
            html_resources = [resource for resource in corpus.resources if resource.kind == "html"]
            self.assertTrue(html_resources)
            first = html_resources[0]
            self.assertTrue(Path(first.html_path).exists())
            self.assertTrue(Path(first.json_path).exists())
            self.assertTrue(Path(first.markdown_path).exists())
            self.assertTrue(Path(first.text_path).exists())
            payload = json.loads(Path(first.json_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["browserless"]["strategy"], "http-fetch")
            self.assertIn("local_metadata", payload)
            self.assertTrue(Path(corpus.manifest_path).exists())
            self.assertTrue(Path(corpus.crawl_index_path).exists())
            self.assertTrue(Path(corpus.quality_report_path).exists())
            self.assertIn(corpus.quality_report.status, {"complete", "partial"})
            document_resources = [resource for resource in corpus.resources if resource.kind == "document"]
            self.assertTrue(document_resources)
            self.assertTrue(Path(document_resources[0].document_path).exists())

    def test_cache_reuse_avoids_second_smart_scrape_call(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_profile("quick_evidence")
            first_browserless = FakeBrowserless()
            MirrorClient(browserless=first_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=Path(tmp),
                run_id="first",
            )
            second_browserless = FakeBrowserless()
            MirrorClient(browserless=second_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=Path(tmp),
                run_id="second",
            )
            self.assertLess(len(second_browserless.scrape_calls), len(first_browserless.scrape_calls))

    def test_low_signal_pages_require_review(self) -> None:
        browserless = FakeBrowserless()
        browserless.scrape_payloads["https://example.com"] = BrowserlessSmartScrapeResult.model_validate(
            {
                "ok": True,
                "statusCode": 200,
                "content": "<html><body>Short</body></html>",
                "contentType": "text/html",
                "headers": {},
                "strategy": "http-fetch",
                "attempted": ["http-fetch"],
                "markdown": "Short",
                "links": [],
            }
        )
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_profile("quick_evidence")
            policy.max_pages = 1
            corpus = MirrorClient(browserless=browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=Path(tmp),
                run_id="review",
            )[0]
            self.assertEqual(corpus.quality_report.status, "review_required")
            self.assertTrue(corpus.quality_report.reasons)

    def test_browserless_budget_caps_map_and_scrape_calls_per_target(self) -> None:
        browserless = FakeBrowserless()
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_profile("quick_evidence")
            policy.max_browserless_calls_per_target = 5
            corpus = MirrorClient(browserless=browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=Path(tmp),
                run_id="budgeted",
                coverage_mode="force",
            )[0]
            self.assertEqual(len(browserless.map_calls) + len(browserless.scrape_calls), 5)
            self.assertEqual(corpus.quality_report.browserless_calls_used, 5)
            self.assertEqual(corpus.quality_report.browserless_call_budget, 5)
            self.assertTrue(corpus.quality_report.budget_exhausted)
            self.assertTrue(any(resource.status == "skipped" for resource in corpus.resources))
            skipped = [entry for entry in corpus.crawl_index if entry.status == "skipped_budget"]
            self.assertTrue(skipped)

    def test_cached_scrape_does_not_spend_budget(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            policy = policy_for_profile("quick_evidence")
            first_browserless = FakeBrowserless()
            MirrorClient(browserless=first_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="first-cache",
                coverage_mode="force",
            )

            cached_policy = policy_for_profile("quick_evidence")
            cached_policy.max_browserless_calls_per_target = 4
            second_browserless = FakeBrowserless()
            corpus = MirrorClient(browserless=second_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=cached_policy,
                workspace_root=workspace,
                run_id="cached-budget",
                coverage_mode="force",
            )[0]
            self.assertEqual(len(second_browserless.map_calls), 4)
            self.assertEqual(second_browserless.scrape_calls, [])
            self.assertEqual(corpus.quality_report.browserless_calls_used, 4)

    def test_associated_document_hosts_are_fetched_from_in_scope_pages(self) -> None:
        browserless = FakeBrowserless()
        browserless.scrape_payloads["https://example.com"] = BrowserlessSmartScrapeResult.model_validate(
            {
                "ok": True,
                "statusCode": 200,
                "content": "<html><body><a href='https://storage.googleapis.com/vendor/manual.pdf'>Manual</a></body></html>",
                "contentType": "text/html",
                "headers": {},
                "strategy": "http-fetch",
                "attempted": ["http-fetch"],
                "markdown": "# Example\n\nManual available.",
                "links": ["https://storage.googleapis.com/vendor/manual.pdf"],
            }
        )
        static = FakeStaticClient(
            {
                "https://storage.googleapis.com/vendor/manual.pdf": FakeStaticResponse(
                    "https://storage.googleapis.com/vendor/manual.pdf",
                    content=b"%PDF-1.4\nnot a real pdf but stored raw",
                    content_type="application/pdf",
                ),
            }
        )
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_profile("quick_evidence")
            policy.max_pages = 1
            corpus = MirrorClient(browserless=browserless, static_client=static).mirror_targets(
                [target],
                policy=policy,
                workspace_root=Path(tmp),
                run_id="associated-docs",
                coverage_mode="force",
            )[0]
            documents = [resource for resource in corpus.resources if resource.kind == "document"]
            manual = next((resource for resource in documents if resource.url == "https://storage.googleapis.com/vendor/manual.pdf"), None)
            self.assertIsNotNone(manual)
            self.assertTrue(Path(manual.document_path).exists())

    def test_run_state_created_before_fetches_and_updates_urls(self) -> None:
        browserless = FakeBrowserless()
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            MirrorClient(browserless=browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy_for_profile("quick_evidence"),
                workspace_root=workspace,
                run_id="stateful",
            )
            state = load_run_state(workspace, "stateful")
            self.assertEqual(state.status, "succeeded")
            target_state = state.target_states[0]
            self.assertEqual(target_state.status, "succeeded")
            self.assertTrue(target_state.selected_urls)
            statuses = {url_state.url: url_state.status for url_state in target_state.urls}
            self.assertEqual(statuses["https://example.com"], "fetched")

    def test_graceful_stop_pauses_after_current_url_and_resume_continues(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_browserless = FakeBrowserless()

            def stop_after_first() -> bool:
                return len(first_browserless.scrape_calls) >= 1

            paused = MirrorClient(browserless=first_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy_for_profile("quick_evidence"),
                workspace_root=workspace,
                run_id="paused",
                stop_requested=stop_after_first,
            )[0]
            self.assertEqual(load_run_state(workspace, "paused").status, "paused")
            self.assertEqual(len([resource for resource in paused.resources if resource.kind == "html"]), 1)

            second_browserless = FakeBrowserless()
            resumed = MirrorClient(browserless=second_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy_for_profile("quick_evidence"),
                workspace_root=workspace,
                run_id="paused",
                resume=True,
            )[0]
            state = load_run_state(workspace, "paused")
            self.assertEqual(state.status, "succeeded")
            self.assertGreater(len(resumed.resources), len(paused.resources))
            self.assertNotIn("https://example.com", second_browserless.scrape_calls)

    def test_resume_does_not_retry_failed_by_default(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        browserless = FakeBrowserless()
        browserless.scrape_payloads["https://example.com"] = BrowserlessSmartScrapeResult.model_validate(
            {
                "ok": False,
                "statusCode": 500,
                "content": "",
                "contentType": "text/html",
                "headers": {},
                "strategy": "http-fetch",
                "attempted": ["http-fetch"],
                "message": "failed",
                "markdown": "",
                "links": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            policy = policy_for_profile("quick_evidence")
            policy.max_pages = 1
            MirrorClient(browserless=browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="failed",
            )
            retry_browserless = FakeBrowserless()
            MirrorClient(browserless=retry_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="failed",
                resume=True,
                retry_failed=False,
            )
            self.assertEqual(retry_browserless.scrape_calls, [])

    def test_complete_run_registers_and_second_run_reuses_by_default(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            policy = policy_for_profile("quick_evidence")
            first_browserless = FakeBrowserless()
            first = MirrorClient(browserless=first_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="first-covered",
            )[0]
            self.assertEqual(first.quality_report.status, "complete")
            self.assertEqual(len(load_registry(workspace).entries), 1)

            second_browserless = FakeBrowserless()
            second = MirrorClient(browserless=second_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="second-covered",
            )[0]
            self.assertEqual(second.run_id, "first-covered")
            self.assertEqual(second_browserless.scrape_calls, [])
            manifest = json.loads((workspace / "output" / "runs" / "second-covered" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["corpora"][0]["disposition"], "reused")

    def test_force_remirrors_and_updates_registry(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            policy = policy_for_profile("quick_evidence")
            MirrorClient(browserless=FakeBrowserless(), static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="first",
            )
            forced_browserless = FakeBrowserless()
            MirrorClient(browserless=forced_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="forced",
                coverage_mode="force",
            )
            self.assertIn("https://example.com", forced_browserless.scrape_calls)
            self.assertEqual(load_registry(workspace).entries[0].run_id, "forced")

    def test_skip_covered_omits_corpus_but_records_manifest_entry(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            policy = policy_for_profile("quick_evidence")
            MirrorClient(browserless=FakeBrowserless(), static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="first",
            )
            skipped_browserless = FakeBrowserless()
            corpora = MirrorClient(browserless=skipped_browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="skipped",
                coverage_mode="skip",
            )
            self.assertEqual(corpora, [])
            self.assertEqual(skipped_browserless.scrape_calls, [])
            manifest = json.loads((workspace / "output" / "runs" / "skipped" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["corpora"][0]["disposition"], "skipped_covered")

    def test_max_age_zero_forces_stale_remirror(self) -> None:
        target = MirrorTarget(target_id="example", display_name="Example", homepage_url="https://example.com")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            policy = policy_for_profile("quick_evidence")
            MirrorClient(browserless=FakeBrowserless(), static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="first",
            )
            browserless = FakeBrowserless()
            MirrorClient(browserless=browserless, static_client=FakeStaticClient()).mirror_targets(
                [target],
                policy=policy,
                workspace_root=workspace,
                run_id="stale",
                max_age_days=0,
            )
            self.assertIn("https://example.com", browserless.scrape_calls)


if __name__ == "__main__":
    unittest.main()
