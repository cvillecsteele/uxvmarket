from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


ProfileName = Literal["quick_evidence", "serious_vendor", "full_audit"]
CoverageMode = Literal["reuse", "skip", "force"]
ResourceKind = Literal["html", "document", "asset", "other"]
ResourceStatus = Literal["planned", "fetched", "failed", "skipped"]
QualityStatus = Literal["complete", "partial", "review_required", "failed"]
PageClass = Literal["homepage", "product", "capability", "company", "contact", "compliance", "document", "news", "career", "other"]
RunStatus = Literal["running", "paused", "succeeded", "failed"]
TargetRunStatus = Literal["pending", "running", "succeeded", "failed"]
UrlRunStatus = Literal["pending", "running", "fetched", "failed", "skipped"]
SkippedReason = Literal[
    "out_of_scope",
    "document_disabled",
    "asset_disabled",
    "budget_exhausted",
    "duplicate",
    "unsupported_scheme",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MirrorTarget(StrictModel):
    target_id: str
    display_name: str
    homepage_url: str
    categories: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    seed_urls: list[str] = Field(
        default_factory=list,
        description="URLs the mirror MUST fetch on this run, regardless of "
        "Browserless /map output and regardless of page_class budgets. "
        "Used by the extract→mirror loop: the extract package emits "
        "fetch_requests per vendor; the followups aggregator surfaces them "
        "via this field. Seeds remain subject to scope, kind filters, and "
        "the per-target Browserless call budget.",
    )

    @field_validator("target_id")
    @classmethod
    def target_id_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("target_id cannot be empty")
        return value


class MirrorPolicy(StrictModel):
    profile: ProfileName = "quick_evidence"
    max_pages: int = 8
    allow_subdomains: bool = False
    include_documents: bool = True
    include_assets: bool = False
    reuse_cache: bool = True
    map_timeout_ms: int = 60_000
    scrape_timeout_ms: int = 60_000
    map_limit_multiplier: int = 8
    min_text_chars: int = 200
    require_contact_or_about: bool = False
    require_product_or_capability: bool = False
    map_queries: list[str] = Field(default_factory=list)
    max_browserless_calls_per_target: int | None = Field(default=None, ge=0)
    allow_associated_document_hosts: bool = True
    associated_document_hosts: list[str] = Field(default_factory=lambda: ["storage.googleapis.com", "storage.cloud.google.com"])
    max_documents_per_target: int = Field(default=25, ge=0)
    page_class_budgets: dict[PageClass, int] = Field(
        default_factory=lambda: {
            "homepage": 1,
            "product": 3,
            "capability": 2,
            "company": 2,
            "contact": 1,
            "compliance": 1,
            "news": 1,
            "career": 0,
            "other": 0,
        }
    )
    max_news_pages: int = Field(default=1, ge=0)
    max_career_pages: int = Field(default=0, ge=0)


class BrowserlessAttempt(StrictModel):
    endpoint: Literal["map", "smart-scrape", "static-http"]
    status: Literal["planned", "succeeded", "failed", "skipped"]
    detail: str | None = None
    strategy: str | None = None
    attempted: list[str] = Field(default_factory=list)


class MirrorResource(StrictModel):
    resource_id: str
    url: str
    final_url: str | None = None
    kind: ResourceKind
    status: ResourceStatus
    content_type: str | None = None
    response_status_code: int | None = None
    html_path: str | None = None
    json_path: str | None = None
    markdown_path: str | None = None
    text_path: str | None = None
    document_path: str | None = None
    sha256: str | None = None
    text_chars: int = 0
    browserless_strategy: str | None = None
    browserless_attempted: list[str] = Field(default_factory=list)
    attempts: list[BrowserlessAttempt] = Field(default_factory=list)
    error_message: str | None = None


class CrawlIndexEntry(StrictModel):
    url: str
    discovered_from: list[str] = Field(default_factory=list)
    depth: int = 0
    in_scope: bool = True
    status: Literal[
        "queued",
        "fetched",
        "failed",
        "skipped_out_of_scope",
        "skipped_document",
        "skipped_asset",
        "skipped_budget",
        "skipped_class_budget",
        "skipped_duplicate",
    ] = "queued"
    final_url: str | None = None
    kind: ResourceKind | None = None
    page_class: PageClass | None = None
    skip_reason: str | None = None
    resource_id: str | None = None


class CrawlLink(StrictModel):
    source_url: str
    target_url: str
    target_in_scope: bool


class QualityReport(StrictModel):
    status: QualityStatus
    fetched_pages: int
    failed_pages: int
    fetched_documents: int
    discovered_urls: int
    skipped_urls: int
    total_text_chars: int
    browserless_calls_used: int = 0
    browserless_call_budget: int | None = None
    budget_exhausted: bool = False
    reasons: list[str] = Field(default_factory=list)


class MirrorCorpus(StrictModel):
    target: MirrorTarget
    policy: MirrorPolicy
    run_id: str
    corpus_root: str
    manifest_path: str
    crawl_index_path: str
    quality_report_path: str
    resources: list[MirrorResource]
    crawl_index: list[CrawlIndexEntry] = Field(default_factory=list)
    crawl_links: list[CrawlLink] = Field(default_factory=list)
    quality_report: QualityReport


class RunManifest(StrictModel):
    run_id: str
    created_at: str = Field(default_factory=utc_now_iso)
    workspace_root: str
    profile: ProfileName
    targets: list[MirrorTarget]
    corpora: list[dict[str, Any]]


class UrlRunState(StrictModel):
    url: str
    status: UrlRunStatus = "pending"
    resource_id: str | None = None
    artifact_manifest_path: str | None = None
    skip_reason: str | None = None
    error_message: str | None = None
    updated_at: str = Field(default_factory=utc_now_iso)


class TargetRunState(StrictModel):
    target_id: str
    status: TargetRunStatus = "pending"
    selected_urls: list[str] = Field(default_factory=list)
    manifest_path: str | None = None
    quality_report_path: str | None = None
    browserless_calls_used: int = 0
    error_message: str | None = None
    urls: list[UrlRunState] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utc_now_iso)


class RunState(StrictModel):
    run_id: str
    status: RunStatus = "running"
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    workspace_root: str
    profile: ProfileName
    policy: MirrorPolicy
    targets: list[MirrorTarget]
    target_states: list[TargetRunState]
    current_target_id: str | None = None
    current_url: str | None = None
    pause_reason: str | None = None


class TargetCoverageEntry(StrictModel):
    target_id: str
    display_name: str
    homepage_url: str
    normalized_homepage_url: str
    profile: ProfileName
    policy_hash: str
    quality_status: QualityStatus
    run_id: str
    corpus_manifest_path: str
    quality_report_path: str
    resource_count: int
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class TargetRegistry(StrictModel):
    schema_version: int = 1
    entries: list[TargetCoverageEntry] = Field(default_factory=list)
