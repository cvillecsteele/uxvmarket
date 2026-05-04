from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class BrowserlessError(RuntimeError):
    pass


class MissingBrowserlessCredentials(BrowserlessError):
    pass


class BrowserlessHttpError(BrowserlessError):
    pass


class BrowserlessMapResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    urls: list[str]
    raw: dict[str, Any]


class BrowserlessSmartScrapeResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ok: bool
    status_code: int | None = Field(default=None, alias="statusCode")
    content: str | dict[str, Any] | None = None
    content_type: str | None = Field(default=None, alias="contentType")
    headers: dict[str, Any] = Field(default_factory=dict)
    strategy: str | None = None
    attempted: list[str] = Field(default_factory=list)
    message: str | None = None
    markdown: str | None = None
    links: list[str] | None = None
    screenshot: str | None = None
    pdf: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class BrowserlessClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("BROWSERLESS_BASE_URL") or "https://production-sfo.browserless.io").rstrip("/")
        self._http_client = http_client

    @classmethod
    def from_env(cls) -> "BrowserlessClient":
        return cls(api_key=os.environ.get("BROWSERLESS_API_KEY") or os.environ.get("BROWSERLESS_TOKEN"))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _require_key(self) -> str:
        if not self.api_key:
            raise MissingBrowserlessCredentials(
                "Set BROWSERLESS_API_KEY or BROWSERLESS_TOKEN to use Browserless-backed mirroring."
            )
        return self.api_key

    def _post_json(self, path: str, payload: dict[str, Any], *, timeout_ms: int) -> Any:
        token = self._require_key()
        endpoint = f"{self.base_url}{path}?token={token}&timeout={timeout_ms}"
        headers = {"Content-Type": "application/json"}
        if self._http_client is not None:
            response = self._http_client.post(endpoint, json=payload, headers=headers)
            if getattr(response, "status_code", 200) >= 400:
                raise BrowserlessHttpError(f"Browserless {path} HTTP {response.status_code}")
            return response.json()
        with httpx.Client(timeout=httpx.Timeout(timeout_ms / 1000.0, connect=15.0), headers=headers) as client:
            response = client.post(endpoint, json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise BrowserlessHttpError(f"Browserless {path} HTTP {response.status_code}") from exc
            return response.json()

    def map_urls(
        self,
        *,
        url: str,
        search: str | None = None,
        limit: int = 100,
        include_subdomains: bool = False,
        include_sitemaps: bool = True,
        timeout_ms: int = 60_000,
    ) -> BrowserlessMapResult:
        payload: dict[str, Any] = {
            "url": url,
            "limit": max(1, min(limit, 5000)),
            "includeSubdomains": include_subdomains,
            "ignoreQueryParameters": True,
            "sitemap": "include" if include_sitemaps else "skip",
        }
        if search:
            payload["search"] = search
        raw = self._post_json("/map", payload, timeout_ms=timeout_ms)
        links = raw.get("links", []) if isinstance(raw, dict) else []
        urls: list[str] = []
        seen: set[str] = set()
        if isinstance(links, list):
            for item in links:
                candidate = item.get("url") if isinstance(item, dict) else item
                if not isinstance(candidate, str) or not candidate.startswith(("http://", "https://")):
                    continue
                if candidate in seen:
                    continue
                seen.add(candidate)
                urls.append(candidate)
        return BrowserlessMapResult(urls=urls, raw=raw if isinstance(raw, dict) else {"raw": raw})

    def smart_scrape(
        self,
        *,
        url: str,
        timeout_ms: int = 60_000,
    ) -> BrowserlessSmartScrapeResult:
        raw = self._post_json(
            "/smart-scrape",
            {"url": url, "formats": ["html", "markdown", "links"]},
            timeout_ms=timeout_ms,
        )
        if not isinstance(raw, dict):
            raw = {"ok": False, "message": "Browserless returned a non-object response", "content": None}
        result = BrowserlessSmartScrapeResult.model_validate(raw)
        result.raw = raw
        return result

