from __future__ import annotations

import unittest

from uxv_mirroring.browserless import BrowserlessClient, MissingBrowserlessCredentials


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts = []
        self.responses = []

    def post(self, endpoint, *, json=None, headers=None):
        self.posts.append((endpoint, json, headers))
        return self.responses.pop(0)


class BrowserlessAdapterTests(unittest.TestCase):
    def test_map_urls_normalizes_links(self) -> None:
        fake = FakeHttpClient()
        fake.responses.append(
            FakeResponse(
                {
                    "success": True,
                    "links": [
                        {"url": "https://example.com"},
                        {"url": "https://example.com/products"},
                        {"url": "mailto:sales@example.com"},
                        "https://example.com/products",
                    ],
                }
            )
        )
        client = BrowserlessClient(api_key="token", base_url="https://browserless.test", http_client=fake)
        result = client.map_urls(url="https://example.com", search="products", limit=10)
        self.assertEqual(result.urls, ["https://example.com", "https://example.com/products"])
        self.assertIn("/map?token=token", fake.posts[0][0])
        self.assertEqual(fake.posts[0][1]["search"], "products")

    def test_smart_scrape_parses_json_wrapper(self) -> None:
        fake = FakeHttpClient()
        fake.responses.append(
            FakeResponse(
                {
                    "ok": True,
                    "statusCode": 200,
                    "content": "<html><body>Hello</body></html>",
                    "contentType": "text/html",
                    "headers": {"content-type": "text/html"},
                    "strategy": "http-fetch",
                    "attempted": ["http-fetch"],
                    "message": None,
                    "markdown": "Hello",
                    "links": ["https://example.com/about"],
                }
            )
        )
        client = BrowserlessClient(api_key="token", base_url="https://browserless.test", http_client=fake)
        result = client.smart_scrape(url="https://example.com")
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.markdown, "Hello")
        self.assertEqual(result.links, ["https://example.com/about"])
        self.assertEqual(fake.posts[0][1]["formats"], ["html", "markdown", "links"])

    def test_missing_credentials_fail_fast(self) -> None:
        client = BrowserlessClient(api_key=None)
        with self.assertRaises(MissingBrowserlessCredentials):
            client.map_urls(url="https://example.com")


if __name__ == "__main__":
    unittest.main()

