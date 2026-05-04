"""Tests for the `seed_urls` / `follow_ups` upgrade.

`MirrorTarget.seed_urls` is a list of URLs the mirror MUST fetch on this
run, regardless of `/map` discovery and regardless of page_class budgets.
This is the primary mechanism for closing the extract→mirror loop: an
extract run produces `fetch_requests` per vendor; the followups aggregator
emits a JSONL where each row carries `follow_ups[].url`; the mirror reads
those URLs (or an explicit `seed_urls` list) and force-includes them.

Seeds are still subject to:
  - scope (homepage domain match unless `allow_subdomains`)
  - kind filters (document/asset disabled by policy)
  - the per-target Browserless call budget
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from uxv_mirroring.browserless import BrowserlessMapResult, BrowserlessSmartScrapeResult
from uxv_mirroring.cli import parse_target_file
from uxv_mirroring.contracts import MirrorTarget
from uxv_mirroring.mirror import MirrorClient, policy_for_profile


class _FakeStaticResponse:
    def __init__(self, url: str, content: bytes = b"", status_code: int = 200, content_type: str = "text/html") -> None:
        self.url = url
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class FakeStaticClient:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url: str):
        self.calls.append(url)
        return _FakeStaticResponse(url=url, content=b"<html></html>")


class FakeBrowserless:
    def __init__(self) -> None:
        self.map_calls = []
        self.scrape_calls = []

    def map_urls(self, *, url, search=None, limit=100, include_subdomains=False, include_sitemaps=True, timeout_ms=60000):
        self.map_calls.append({"url": url, "search": search})
        return BrowserlessMapResult(urls=[url], raw={})

    def smart_scrape(self, *, url, timeout_ms=60000):
        self.scrape_calls.append(url)
        return BrowserlessSmartScrapeResult.model_validate(
            {
                "ok": True,
                "statusCode": 200,
                "content": f"<html><body><h1>{url}</h1></body></html>",
                "contentType": "text/html",
                "headers": {"content-type": "text/html"},
                "strategy": "http-fetch",
                "attempted": ["http-fetch"],
                "markdown": (
                    f"# {url}\n\nVendor evidence content for UAS / drone "
                    "products and manufacturing on this page."
                ),
                "links": [],
            }
        )


class SeedUrlContractTests(unittest.TestCase):
    def test_mirror_target_defaults_seed_urls_to_empty(self) -> None:
        target = MirrorTarget(
            target_id="example",
            display_name="Example",
            homepage_url="https://example.com",
        )
        self.assertEqual(target.seed_urls, [])

    def test_mirror_target_round_trip_with_seed_urls(self) -> None:
        target = MirrorTarget(
            target_id="example",
            display_name="Example",
            homepage_url="https://example.com",
            seed_urls=[
                "https://example.com/products",
                "https://example.com/about",
            ],
        )
        clone = MirrorTarget.model_validate(target.model_dump())
        self.assertEqual(clone.seed_urls, target.seed_urls)


class ParseTargetFileTests(unittest.TestCase):
    def test_explicit_seed_urls_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "display_name": "Example",
                        "homepage_url": "https://example.com",
                        "seed_urls": [
                            "https://example.com/products",
                            "https://example.com/about",
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            targets = parse_target_file(path)
        self.assertEqual(len(targets), 1)
        self.assertEqual(
            targets[0].seed_urls,
            [
                "https://example.com/products",
                "https://example.com/about",
            ],
        )

    def test_extract_followups_field_is_lifted_into_seed_urls(self) -> None:
        """The `extract/` package emits `follow_ups: [{url, ...}]`. The mirror
        reads only `url` from each entry and treats the result as seed URLs."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "followups.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "display_name": "Example",
                        "homepage_url": "https://example.com",
                        "follow_ups": [
                            {
                                "url": "https://example.com/products",
                                "reason": "agent wants product list",
                                "expected_evidence": ["products"],
                                "in_corpus_index": True,
                            },
                            {
                                "url": "https://example.com/about",
                                "reason": "headquarters details",
                                "expected_evidence": ["headquarters"],
                                "in_corpus_index": False,
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            targets = parse_target_file(path)
        self.assertEqual(
            targets[0].seed_urls,
            [
                "https://example.com/products",
                "https://example.com/about",
            ],
        )

    def test_seed_urls_and_follow_ups_are_merged_and_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "display_name": "Example",
                        "homepage_url": "https://example.com",
                        "seed_urls": ["https://example.com/products"],
                        "follow_ups": [
                            {"url": "https://example.com/products"},
                            {"url": "https://example.com/about"},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            targets = parse_target_file(path)
        self.assertEqual(
            targets[0].seed_urls,
            [
                "https://example.com/products",
                "https://example.com/about",
            ],
        )


class SeedUrlMirrorTests(unittest.TestCase):
    def _run(
        self,
        target: MirrorTarget,
        *,
        map_urls: list[str],
        tmp: str,
    ) -> tuple:
        browserless = FakeBrowserless()
        urls_for_map = list(map_urls)

        def map_urls_fn(*, url, search=None, limit=100, include_subdomains=False, include_sitemaps=True, timeout_ms=60000):
            browserless.map_calls.append({"url": url, "search": search})
            return BrowserlessMapResult(urls=urls_for_map, raw={})

        browserless.map_urls = map_urls_fn  # type: ignore[method-assign]
        policy = policy_for_profile("quick_evidence")
        # Force every page_class budget to 0 so the only way a seed gets
        # fetched is via the seed-URL bypass.
        policy.max_pages = 1  # only homepage
        for cls in policy.page_class_budgets:
            policy.page_class_budgets[cls] = 0
        policy.page_class_budgets["homepage"] = 1
        policy.max_browserless_calls_per_target = 50
        client = MirrorClient(browserless=browserless, static_client=FakeStaticClient())
        corpora = client.mirror_targets(
            [target],
            policy=policy,
            workspace_root=Path(tmp),
            run_id="seed-test",
            coverage_mode="force",
        )
        return browserless, corpora[0]

    def test_seed_url_is_fetched_even_when_class_budget_is_zero(self) -> None:
        target = MirrorTarget(
            target_id="example",
            display_name="Example",
            homepage_url="https://example.com",
            seed_urls=["https://example.com/special-product"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            browserless, corpus = self._run(
                target,
                map_urls=["https://example.com", "https://example.com/about"],
                tmp=tmp,
            )
        fetched_urls = {r.url for r in corpus.resources if r.status == "fetched"}
        self.assertIn("https://example.com/special-product", fetched_urls)
        self.assertIn("https://example.com", fetched_urls)
        # /about should be skipped — class budget is 0 and it's not a seed:
        self.assertNotIn("https://example.com/about", fetched_urls)

    def test_seed_url_appears_in_crawl_index_with_followup_provenance(self) -> None:
        target = MirrorTarget(
            target_id="example",
            display_name="Example",
            homepage_url="https://example.com",
            seed_urls=["https://example.com/special-product"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, corpus = self._run(
                target,
                map_urls=["https://example.com"],
                tmp=tmp,
            )
        seed_entry = next(
            entry for entry in corpus.crawl_index
            if entry.url == "https://example.com/special-product"
        )
        self.assertEqual(seed_entry.status, "fetched")
        self.assertIn("follow_up:seed", seed_entry.discovered_from)

    def test_seed_url_equal_to_homepage_does_not_double_fetch(self) -> None:
        target = MirrorTarget(
            target_id="example",
            display_name="Example",
            homepage_url="https://example.com",
            seed_urls=["https://example.com"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            browserless, corpus = self._run(
                target,
                map_urls=["https://example.com"],
                tmp=tmp,
            )
        scrape_calls_for_home = [
            url for url in browserless.scrape_calls if url == "https://example.com"
        ]
        self.assertEqual(len(scrape_calls_for_home), 1)
        homepage_resources = [
            r for r in corpus.resources if r.url == "https://example.com"
        ]
        self.assertEqual(len(homepage_resources), 1)


if __name__ == "__main__":
    unittest.main()
