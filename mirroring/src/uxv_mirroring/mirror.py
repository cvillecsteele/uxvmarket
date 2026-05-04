from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from uxv_mirroring.browserless import BrowserlessClient, BrowserlessSmartScrapeResult
from uxv_mirroring.contracts import (
    BrowserlessAttempt,
    CoverageMode,
    CrawlIndexEntry,
    CrawlLink,
    MirrorCorpus,
    MirrorPolicy,
    MirrorResource,
    MirrorTarget,
    PageClass,
    ProfileName,
    QualityReport,
    RunManifest,
    RunState,
)
from uxv_mirroring.materialize import (
    json_safe_browserless_response,
    parse_pdf_text,
    sha256_bytes,
    sha256_text,
    slugify,
    text_from_markdown_or_html,
    write_bytes,
    write_json,
    write_text,
)
from uxv_mirroring.state import (
    initialize_run_state,
    load_run_state,
    mark_target,
    mark_url,
    recover_running_work,
    save_run_state,
    set_selected_urls,
    target_state_for,
    validate_unique_targets,
)
from uxv_mirroring.registry import find_covered_entry, load_registry, update_registry_for_corpus


DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"}
STATIC_EXTENSIONS = {".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".mp4", ".mov"}
PRODUCT_TOKENS = ("product", "products", "catalog", "datasheet", "spec", "specs", "solution", "solutions", "capabilities")
COMPLIANCE_TOKENS = ("compliance", "certification", "certifications", "quality", "itar", "ndaa", "blue-uas", "as9100")
ABOUT_TOKENS = ("about", "company", "team", "leadership", "contact", "location", "headquarters", "facility")
LOW_VALUE_TOKENS = ("blog", "news", "press", "career", "careers", "jobs", "event", "webinar")
PAGE_CLASS_ORDER: tuple[PageClass, ...] = ("homepage", "product", "capability", "company", "contact", "compliance", "news", "career", "other")
DEFAULT_PAGE_CLASS_BUDGETS: dict[ProfileName, dict[PageClass, int]] = {
    "quick_evidence": {
        "homepage": 1,
        "product": 3,
        "capability": 2,
        "company": 2,
        "contact": 1,
        "compliance": 1,
        "news": 1,
        "career": 0,
        "other": 0,
    },
    "serious_vendor": {
        "homepage": 1,
        "product": 18,
        "capability": 14,
        "company": 7,
        "contact": 2,
        "compliance": 5,
        "news": 3,
        "career": 0,
        "other": 0,
    },
    "full_audit": {
        "homepage": 1,
        "product": 160,
        "capability": 120,
        "company": 35,
        "contact": 10,
        "compliance": 35,
        "news": 100,
        "career": 5,
        "other": 34,
    },
}
NEWS_TOKENS = ("blog", "news", "press", "media", "article", "announcement", "announces", "partnership", "partner", "award", "mou", "sign", "teaming")
CAREER_TOKENS = ("career", "careers", "job", "jobs", "recruiting", "life-at", "join-us")
LEGAL_COMPANY_TOKENS = ("terms", "terms-of-service", "privacy", "privacy-policy", "legal", "cookies", "cookie", "gdpr", "data-protection")
CLEAR_PRODUCT_TOKENS = ("product", "products", "platform", "platforms", "v-bat", "hivemind", "nova", "ai-pilot")
CANONICAL_PRODUCT_SLUGS = ("products", "v-bat", "v-bat-teams", "hivemind", "hivemind-solutions", "hivemind-enterprise", "ai-pilot")
CAPABILITY_TOKENS = ("autonomy", "autonomous", "mission", "capability", "capabilities", "technology", "solution", "solutions", "air", "ground", "systems", "ai")
CATEGORY_QUERY_TERMS: dict[str, list[str]] = {
    "communications": ["datalink", "radio", "mesh communications"],
    "flight_vehicle_control": ["autopilot", "flight controller", "avionics"],
    "isr_payloads": ["EO IR payload", "gimbal", "imaging payload"],
    "power_systems": ["battery", "power system", "charger"],
    "propulsion_mechanical": ["motor", "propeller", "propulsion"],
    "propulsion_electronics": ["ESC", "motor controller"],
    "sensors_navigation": ["IMU", "GNSS", "navigation"],
    "ground_segment": ["ground control station", "antenna", "launcher"],
}


class BrowserlessLike(Protocol):
    def map_urls(
        self,
        *,
        url: str,
        search: str | None = None,
        limit: int = 100,
        include_subdomains: bool = False,
        include_sitemaps: bool = True,
        timeout_ms: int = 60_000,
    ): ...

    def smart_scrape(self, *, url: str, timeout_ms: int = 60_000) -> BrowserlessSmartScrapeResult: ...


@dataclass(frozen=True)
class PreflightResult:
    final_url: str
    status_code: int | None
    content_type: str | None
    error: str | None = None


@dataclass
class BrowserlessCallBudget:
    max_calls: int | None
    used: int = 0

    @property
    def remaining(self) -> int | None:
        if self.max_calls is None:
            return None
        return max(self.max_calls - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.max_calls is not None and self.used >= self.max_calls

    def can_spend(self) -> bool:
        return not self.exhausted

    def spend(self) -> None:
        self.used += 1


def policy_for_profile(profile: ProfileName) -> MirrorPolicy:
    if profile == "quick_evidence":
        return MirrorPolicy(
            profile=profile,
            max_pages=8,
            include_documents=True,
            max_browserless_calls_per_target=10,
            page_class_budgets=DEFAULT_PAGE_CLASS_BUDGETS[profile],
            max_news_pages=1,
            max_career_pages=0,
        )
    if profile == "serious_vendor":
        return MirrorPolicy(
            profile=profile,
            max_pages=50,
            include_documents=True,
            require_contact_or_about=True,
            require_product_or_capability=True,
            max_browserless_calls_per_target=80,
            page_class_budgets=DEFAULT_PAGE_CLASS_BUDGETS[profile],
            max_news_pages=3,
            max_career_pages=0,
        )
    return MirrorPolicy(
        profile=profile,
        max_pages=500,
        include_documents=True,
        require_contact_or_about=True,
        require_product_or_capability=True,
        max_browserless_calls_per_target=650,
        page_class_budgets=DEFAULT_PAGE_CLASS_BUDGETS[profile],
        max_news_pages=100,
        max_career_pages=5,
    )


def normalize_url(url: str) -> str:
    normalized, _fragment = urldefrag(url)
    return normalized.rstrip("/") if normalized.endswith("/") and urlparse(normalized).path != "/" else normalized


def path_extension(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


def resource_kind(url: str, content_type: str | None = None) -> str:
    extension = path_extension(url)
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    if extension in STATIC_EXTENSIONS:
        return "asset"
    content = (content_type or "").split(";", 1)[0].strip().lower()
    if content == "application/pdf":
        return "document"
    if content in {"text/html", "application/xhtml+xml", ""}:
        return "html"
    return "other"


def is_in_scope(url: str, home_url: str, *, allow_subdomains: bool) -> bool:
    candidate = urlparse(url)
    home = urlparse(home_url)
    if candidate.scheme not in {"http", "https"}:
        return False
    candidate_host = (candidate.hostname or "").lower()
    home_host = (home.hostname or "").lower()
    candidate_apex = candidate_host.removeprefix("www.")
    home_apex = home_host.removeprefix("www.")
    if candidate_host == home_host:
        return True
    if candidate_apex == home_apex and (candidate_host == f"www.{home_apex}" or home_host == f"www.{candidate_apex}"):
        return True
    return allow_subdomains and candidate_apex.endswith(f".{home_apex}")


def is_associated_document_url(url: str, policy: MirrorPolicy) -> bool:
    if not policy.include_documents or not policy.allow_associated_document_hosts:
        return False
    if resource_kind(url) != "document":
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == allowed.lower() or host.endswith(f".{allowed.lower()}") for allowed in policy.associated_document_hosts)


def is_fetchable_url(url: str, home_url: str, *, policy: MirrorPolicy) -> bool:
    return is_in_scope(url, home_url, allow_subdomains=policy.allow_subdomains) or is_associated_document_url(url, policy)


def _url_terms(url: str) -> tuple[str, list[str]]:
    parsed = urlparse(url)
    path = parsed.path.strip("/").lower()
    terms = [segment for segment in path.replace("_", "-").split("/") if segment]
    return path, terms


def _contains_any(value: str, tokens: tuple[str, ...]) -> bool:
    return any(token in value for token in tokens)


def _contains_token(value: str, token: str) -> bool:
    normalized_token = re.escape(token.lower()).replace("\\ ", "[- ]")
    return re.search(rf"(?<![a-z0-9]){normalized_token}(?![a-z0-9])", value.lower()) is not None


def _contains_any_token(value: str, tokens: tuple[str, ...]) -> bool:
    return any(_contains_token(value, token) for token in tokens)


def _is_long_article_slug(path: str) -> bool:
    leaf = path.rsplit("/", 1)[-1]
    return leaf.count("-") >= 5


def _has_segment(terms: list[str], tokens: tuple[str, ...]) -> bool:
    return any(term in tokens for term in terms)


def classify_page(url: str, *, home_url: str, policy: MirrorPolicy) -> PageClass:
    kind = resource_kind(url)
    if kind == "document":
        return "document"
    if normalize_url(url) == normalize_url(home_url):
        return "homepage"
    path, terms = _url_terms(url)
    leaf = terms[-1] if terms else ""
    if not path:
        return "homepage"
    if _contains_any(path, CAREER_TOKENS):
        return "career"
    if _contains_any(path, ("contact", "locations", "headquarters")):
        return "contact"
    if _contains_any(path, LEGAL_COMPANY_TOKENS) or _has_segment(terms, ("about", "company", "team", "leadership", "executive", "executives", "facility", "facilities")):
        return "company"
    if _contains_any_token(path, COMPLIANCE_TOKENS) or _contains_any_token(path, ("security", "procurement", "government")):
        return "compliance"
    canonical_product = leaf in CANONICAL_PRODUCT_SLUGS or leaf.endswith("product-page")
    article_like = _contains_any(path, NEWS_TOKENS) or _is_long_article_slug(path)
    if canonical_product:
        return "product"
    if article_like:
        return "news"
    if _contains_any_token(path, CLEAR_PRODUCT_TOKENS):
        return "product"
    if _contains_any_token(path, CAPABILITY_TOKENS):
        return "capability"
    if any(token in leaf for token in ("datasheet", "spec", "manual", "catalog")):
        return "product"
    return "other"


def page_class_budget(policy: MirrorPolicy, page_class: PageClass) -> int:
    if page_class == "document":
        return policy.max_documents_per_target
    if page_class == "news":
        return min(policy.page_class_budgets.get("news", policy.max_news_pages), policy.max_news_pages)
    if page_class == "career":
        return min(policy.page_class_budgets.get("career", policy.max_career_pages), policy.max_career_pages)
    return policy.page_class_budgets.get(page_class, 0)


def page_score(url: str, categories: list[str]) -> int:
    lowered = url.lower()
    score = 0
    path_depth = len([segment for segment in urlparse(url).path.split("/") if segment])
    leaf = urlparse(url).path.strip("/").rsplit("/", 1)[-1]
    if path_depth <= 1:
        score += 20
    if leaf.count("-") >= 5:
        score -= 35
    if any(token in lowered for token in PRODUCT_TOKENS):
        score += 70
    if any(token in lowered for token in COMPLIANCE_TOKENS):
        score += 55
    if any(token in lowered for token in ABOUT_TOKENS):
        score += 35
    for category in categories:
        for token in CATEGORY_QUERY_TERMS.get(category, []):
            if token.lower().replace(" ", "-") in lowered or token.lower().replace(" ", "") in lowered:
                score += 25
    if any(token in lowered for token in LOW_VALUE_TOKENS):
        score -= 50
    if resource_kind(url) == "document":
        score += 20
    return score - min(path_depth, 8)


def map_queries_for(target: MirrorTarget, policy: MirrorPolicy) -> list[str | None]:
    queries: list[str | None] = [None, "products capabilities", "compliance certifications", "about contact headquarters"]
    for category in target.categories:
        queries.extend(CATEGORY_QUERY_TERMS.get(category, []))
    queries.extend(policy.map_queries)
    seen: set[str | None] = set()
    ordered: list[str | None] = []
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        ordered.append(query)
    if policy.profile == "quick_evidence":
        return ordered[:4]
    if policy.profile == "serious_vendor":
        return ordered[:8]
    return ordered


class MirrorClient:
    def __init__(
        self,
        *,
        browserless: BrowserlessLike | None = None,
        static_client: httpx.Client | None = None,
    ) -> None:
        self.browserless = browserless or BrowserlessClient.from_env()
        self.static_client = static_client

    def mirror_targets(
        self,
        targets: list[MirrorTarget],
        *,
        policy: MirrorPolicy,
        workspace_root: Path,
        run_id: str | None = None,
        resume: bool = False,
        retry_failed: bool = False,
        stop_requested=None,
        coverage_mode: CoverageMode = "reuse",
        max_age_days: int | None = None,
    ) -> list[MirrorCorpus]:
        if resume:
            if run_id is None:
                raise ValueError("run_id is required when resume=True")
            state = recover_running_work(load_run_state(workspace_root, run_id), retry_failed=retry_failed)
            targets = state.targets
            policy = state.policy
        else:
            validate_unique_targets(targets)
            run_id = run_id or uuid.uuid4().hex[:12]
            state = initialize_run_state(
                run_id=run_id,
                workspace_root=workspace_root,
                targets=targets,
                policy=policy,
            )
        save_run_state(workspace_root, state)
        run_root = workspace_root / "output" / "runs" / run_id
        corpora: list[MirrorCorpus] = []
        manifest_entries: list[dict[str, Any]] = []
        registry = load_registry(workspace_root) if not resume and coverage_mode != "force" else None
        for target in targets:
            if not resume and registry is not None:
                covered = find_covered_entry(registry, target=target, policy=policy, max_age_days=max_age_days)
                if covered is not None:
                    if coverage_mode == "reuse":
                        existing = self._load_existing_corpus(Path(covered.corpus_manifest_path))
                        if existing is not None:
                            corpora.append(existing)
                            manifest_entries.append(
                                self._manifest_entry(
                                    existing,
                                    disposition="reused",
                                    covered_by_run_id=covered.run_id,
                                )
                            )
                            continue
                    elif coverage_mode == "skip":
                        manifest_entries.append(
                            {
                                "target_id": target.target_id,
                                "display_name": target.display_name,
                                "quality_status": covered.quality_status,
                                "manifest_path": covered.corpus_manifest_path,
                                "quality_report_path": covered.quality_report_path,
                                "resource_count": covered.resource_count,
                                "disposition": "skipped_covered",
                                "covered_by_run_id": covered.run_id,
                            }
                        )
                        mark_target(state, target.target_id, "succeeded")
                        save_run_state(workspace_root, state)
                        continue
            target_state = target_state_for(state, target.target_id)
            if target_state.status == "succeeded" and target_state.manifest_path:
                existing = self._load_existing_corpus(Path(target_state.manifest_path))
                if existing is not None:
                    corpora.append(existing)
                    manifest_entries.append(self._manifest_entry(existing, disposition="mirrored"))
                continue
            corpus = self._mirror_one(
                target,
                policy=policy,
                run_root=run_root,
                workspace_root=workspace_root,
                run_id=run_id,
                state=state,
                retry_failed=retry_failed,
                stop_requested=stop_requested,
                ignore_url_cache=(not resume and (coverage_mode == "force" or max_age_days is not None)),
            )
            corpora.append(corpus)
            manifest_entries.append(self._manifest_entry(corpus, disposition="mirrored" if corpus.quality_report.status != "failed" else "failed"))
            update_registry_for_corpus(workspace_root, corpus)
            if state.status == "paused":
                break
        if state.status != "paused":
            if all(target_state.status == "succeeded" for target_state in state.target_states):
                state.status = "succeeded"
                state.current_target_id = None
                state.current_url = None
                state.pause_reason = None
            else:
                state.status = "running"
        manifest = RunManifest(
            run_id=run_id,
            workspace_root=str(workspace_root),
            profile=policy.profile,
            targets=targets,
            corpora=manifest_entries,
        )
        write_json(run_root / "manifest.json", manifest.model_dump())
        save_run_state(workspace_root, state)
        return corpora

    def _manifest_entry(
        self,
        corpus: MirrorCorpus,
        *,
        disposition: str,
        covered_by_run_id: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "target_id": corpus.target.target_id,
            "display_name": corpus.target.display_name,
            "quality_status": corpus.quality_report.status,
            "manifest_path": corpus.manifest_path,
            "quality_report_path": corpus.quality_report_path,
            "resource_count": len(corpus.resources),
            "disposition": disposition,
            "browserless_calls_used": corpus.quality_report.browserless_calls_used,
            "browserless_call_budget": corpus.quality_report.browserless_call_budget,
            "budget_exhausted": corpus.quality_report.budget_exhausted,
        }
        if covered_by_run_id is not None:
            entry["covered_by_run_id"] = covered_by_run_id
        return entry

    def _mirror_one(
        self,
        target: MirrorTarget,
        *,
        policy: MirrorPolicy,
        run_root: Path,
        workspace_root: Path,
        run_id: str,
        state: RunState,
        retry_failed: bool,
        stop_requested,
        ignore_url_cache: bool,
    ) -> MirrorCorpus:
        target_root = run_root / "targets" / slugify(target.target_id)
        target_state = target_state_for(state, target.target_id)
        budget = BrowserlessCallBudget(policy.max_browserless_calls_per_target, target_state.browserless_calls_used)
        mark_target(state, target.target_id, "running")
        save_run_state(workspace_root, state)

        existing = self._load_existing_corpus(target_root / "manifest.json")
        resources: list[MirrorResource] = list(existing.resources) if existing is not None else []
        crawl_index: dict[str, CrawlIndexEntry] = {entry.url: entry for entry in existing.crawl_index} if existing is not None else {}
        crawl_links: list[CrawlLink] = list(existing.crawl_links) if existing is not None else []
        resource_by_url = {resource.url: resource for resource in resources}
        resource_by_url.update({resource.final_url: resource for resource in resources if resource.final_url})

        if target_state.selected_urls:
            selected = target_state.selected_urls
            home_url = normalize_url(target.homepage_url)
            for url in selected:
                crawl_index.setdefault(
                    url,
                    CrawlIndexEntry(url=url, kind=resource_kind(url), page_class=classify_page(url, home_url=home_url, policy=policy)),  # type: ignore[arg-type]
                )
        else:
            preflight = self._preflight(target.homepage_url)
            home_url = normalize_url(preflight.final_url or target.homepage_url)
            discovered, discovered_index, discovered_links = self._discover_urls(target, policy=policy, home_url=home_url, budget=budget)
            target_state.browserless_calls_used = budget.used
            crawl_index.update(discovered_index)
            crawl_links.extend(discovered_links)
            selected = self._select_urls(
                discovered,
                target=target,
                policy=policy,
                home_url=home_url,
                crawl_index=crawl_index,
                seed_urls=target.seed_urls,
            )
            set_selected_urls(target_state, selected)
            save_run_state(workspace_root, state)

        for url in selected:
            url_state = next((item for item in target_state.urls if item.url == url), None)
            if url_state is not None and url_state.status in {"fetched", "skipped"} and url in resource_by_url:
                continue
            if url_state is not None and url_state.status == "failed" and not retry_failed:
                continue
            ordinal = len(resources) + 1
            kind = resource_kind(url)
            mark_url(state, target.target_id, url, "running")
            save_run_state(workspace_root, state)
            if kind == "document":
                resource = self._fetch_document(url, target_root=target_root, workspace_root=workspace_root, ordinal=ordinal)
            elif kind == "asset" and not policy.include_assets:
                crawl_index[url].status = "skipped_asset"
                crawl_index[url].skip_reason = "assets disabled by mirror policy"
                mark_url(state, target.target_id, url, "skipped", skip_reason="assets disabled by mirror policy")
                save_run_state(workspace_root, state)
                continue
            else:
                resource = self._fetch_html(
                    url,
                    target_root=target_root,
                    workspace_root=workspace_root,
                    policy=policy,
                    ordinal=ordinal,
                    budget=budget,
                    ignore_cache=ignore_url_cache,
                )
                target_state.browserless_calls_used = budget.used
                for link in self._links_from_resource(resource):
                    linked = normalize_url(urljoin(resource.final_url or resource.url, link))
                    fetchable = is_fetchable_url(linked, home_url, policy=policy)
                    crawl_links.append(CrawlLink(source_url=resource.final_url or resource.url, target_url=linked, target_in_scope=fetchable))
                    source_url = resource.final_url or resource.url
                    if linked not in crawl_index:
                        entry = CrawlIndexEntry(
                            url=linked,
                            discovered_from=[source_url],
                            depth=1,
                            in_scope=fetchable,
                            status="queued" if fetchable else "skipped_out_of_scope",
                            skip_reason=None if fetchable else "outside target scope",
                            kind=resource_kind(linked),  # type: ignore[arg-type]
                            page_class=classify_page(linked, home_url=home_url, policy=policy),
                        )
                        crawl_index[linked] = entry
                    else:
                        entry = crawl_index[linked]
                        if source_url not in entry.discovered_from:
                            entry.discovered_from.append(source_url)
                        entry.page_class = entry.page_class or classify_page(linked, home_url=home_url, policy=policy)
                        if fetchable and entry.status == "skipped_out_of_scope":
                            entry.in_scope = True
                            entry.status = "queued"
                            entry.skip_reason = None
                    if (
                        is_associated_document_url(linked, policy)
                        and linked not in selected
                        and linked not in resource_by_url
                        and sum(1 for item in selected if resource_kind(item) == "document") < policy.max_documents_per_target
                    ):
                        selected.append(linked)
                        if target_state.selected_urls is not selected:
                            target_state.selected_urls.append(linked)
                        set_selected_urls(target_state, target_state.selected_urls)
                        save_run_state(workspace_root, state)
            resources.append(resource)
            resource_by_url[url] = resource
            if resource.final_url:
                resource_by_url[resource.final_url] = resource
            entry = crawl_index.setdefault(url, CrawlIndexEntry(url=url))
            if resource.status == "fetched":
                entry.status = "fetched"
            elif resource.status == "skipped":
                entry.status = "skipped_budget"
                entry.skip_reason = resource.error_message or "Browserless call budget exhausted"
            else:
                entry.status = "failed"
            entry.final_url = resource.final_url
            entry.kind = resource.kind
            entry.resource_id = resource.resource_id
            mark_url(
                state,
                target.target_id,
                url,
                "fetched" if resource.status == "fetched" else "skipped" if resource.status == "skipped" else "failed",
                resource_id=resource.resource_id,
                skip_reason=resource.error_message if resource.status == "skipped" else None,
                error_message=resource.error_message,
            )
            save_run_state(workspace_root, state)
            if stop_requested is not None and stop_requested():
                state.status = "paused"
                state.pause_reason = "stop requested"
                state.current_target_id = target.target_id
                state.current_url = None
                save_run_state(workspace_root, state)
                break

        paused = state.status == "paused"
        for entry in crawl_index.values():
            if entry.status == "queued" and not paused:
                page_class = entry.page_class or classify_page(entry.url, home_url=home_url, policy=policy)
                entry.page_class = page_class
                if page_class != "document" and page_class_budget(policy, page_class) <= 0:
                    entry.status = "skipped_class_budget"
                    entry.skip_reason = f"{page_class} class budget exhausted"
                else:
                    entry.status = "skipped_budget"
                    entry.skip_reason = f"not selected within max_pages={policy.max_pages}"

        target_state.browserless_calls_used = budget.used
        quality = self._quality_report(resources, crawl_index, target=target, policy=policy, browserless_calls_used=budget.used)
        crawl_index_path = target_root / "crawl_index.json"
        quality_path = target_root / "quality_report.json"
        manifest_path = target_root / "manifest.json"
        sorted_entries = sorted(crawl_index.values(), key=lambda item: (item.depth, item.url))
        write_json(crawl_index_path, {"target": target.model_dump(), "entries": [entry.model_dump() for entry in sorted_entries], "links": [link.model_dump() for link in crawl_links]})
        write_json(quality_path, quality.model_dump())

        corpus = MirrorCorpus(
            target=target,
            policy=policy,
            run_id=run_id,
            corpus_root=str(target_root),
            manifest_path=str(manifest_path),
            crawl_index_path=str(crawl_index_path),
            quality_report_path=str(quality_path),
            resources=resources,
            crawl_index=sorted_entries,
            crawl_links=crawl_links,
            quality_report=quality,
        )
        write_json(manifest_path, corpus.model_dump())
        target_state.manifest_path = str(manifest_path)
        target_state.quality_report_path = str(quality_path)
        if not paused:
            mark_target(
                state,
                target.target_id,
                "succeeded" if quality.status != "failed" else "failed",
                error_message=None if quality.status != "failed" else "; ".join(quality.reasons),
            )
        save_run_state(workspace_root, state)
        return corpus

    def _load_existing_corpus(self, manifest_path: Path) -> MirrorCorpus | None:
        if not manifest_path.exists():
            return None
        try:
            return MirrorCorpus.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _preflight(self, url: str) -> PreflightResult:
        client = self.static_client
        close_client = False
        if client is None:
            client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(15.0, connect=5.0))
            close_client = True
        try:
            response = client.get(url)
            return PreflightResult(
                final_url=str(getattr(response, "url", url)),
                status_code=getattr(response, "status_code", None),
                content_type=getattr(response, "headers", {}).get("content-type") if hasattr(response, "headers") else None,
            )
        except Exception as exc:
            return PreflightResult(final_url=url, status_code=None, content_type=None, error=str(exc))
        finally:
            if close_client:
                client.close()

    def _discover_urls(
        self,
        target: MirrorTarget,
        *,
        policy: MirrorPolicy,
        home_url: str,
        budget: BrowserlessCallBudget,
    ) -> tuple[list[str], dict[str, CrawlIndexEntry], list[CrawlLink]]:
        discovered = [home_url]
        crawl_index: dict[str, CrawlIndexEntry] = {
            home_url: CrawlIndexEntry(url=home_url, depth=0, kind="html", page_class="homepage")
        }
        crawl_links: list[CrawlLink] = []
        limit = min(max(policy.max_pages * policy.map_limit_multiplier, policy.max_pages), 5000)
        for query in map_queries_for(target, policy):
            if not budget.can_spend():
                crawl_index[f"browserless:map:{query or 'broad'}"] = CrawlIndexEntry(
                    url=f"browserless:map:{query or 'broad'}",
                    status="skipped_budget",
                    skip_reason=f"Browserless call budget exhausted after {budget.used} call(s)",
                )
                continue
            budget.spend()
            try:
                result = self.browserless.map_urls(
                    url=home_url,
                    search=query,
                    limit=limit,
                    include_subdomains=policy.allow_subdomains,
                    include_sitemaps=True,
                    timeout_ms=policy.map_timeout_ms,
                )
            except Exception as exc:
                crawl_index[f"browserless:map:{query or 'broad'}"] = CrawlIndexEntry(
                    url=f"browserless:map:{query or 'broad'}",
                    status="failed",
                    skip_reason=str(exc),
                )
                continue
            for raw_url in result.urls:
                url = normalize_url(raw_url)
                if url not in discovered:
                    discovered.append(url)
                in_scope = is_in_scope(url, home_url, allow_subdomains=policy.allow_subdomains)
                entry = crawl_index.setdefault(
                    url,
                    CrawlIndexEntry(
                        url=url,
                        discovered_from=[f"browserless:map:{query or 'broad'}"],
                        depth=1,
                        in_scope=in_scope,
                        status="queued" if in_scope else "skipped_out_of_scope",
                        skip_reason=None if in_scope else "outside target scope",
                        kind=resource_kind(url),  # type: ignore[arg-type]
                        page_class=classify_page(url, home_url=home_url, policy=policy),
                    ),
                )
                entry.page_class = entry.page_class or classify_page(url, home_url=home_url, policy=policy)
                source = f"browserless:map:{query or 'broad'}"
                if source not in entry.discovered_from:
                    entry.discovered_from.append(source)
        return discovered, crawl_index, crawl_links

    def _select_urls(
        self,
        urls: list[str],
        *,
        target: MirrorTarget,
        policy: MirrorPolicy,
        home_url: str,
        crawl_index: dict[str, CrawlIndexEntry],
        seed_urls: list[str] | None = None,
    ) -> list[str]:
        selected: list[str] = []
        seen: set[str] = set()
        selected_by_class: dict[PageClass, int] = {page_class: 0 for page_class in PAGE_CLASS_ORDER}

        def include(url: str, *, enforce_class_budget: bool = True) -> bool:
            page_class = classify_page(url, home_url=home_url, policy=policy)
            entry = crawl_index.setdefault(url, CrawlIndexEntry(url=url))
            entry.page_class = entry.page_class or page_class
            entry.kind = entry.kind or resource_kind(url)  # type: ignore[assignment]
            if url in seen:
                if url in crawl_index:
                    crawl_index[url].status = "skipped_duplicate"
                    crawl_index[url].skip_reason = "duplicate URL"
                return False
            kind = resource_kind(url)
            link_discovered_associated_document = (
                is_associated_document_url(url, policy)
                and any(not source.startswith("browserless:map:") for source in entry.discovered_from)
            )
            if not is_in_scope(url, home_url, allow_subdomains=policy.allow_subdomains) and not link_discovered_associated_document:
                entry.status = "skipped_out_of_scope"
                entry.skip_reason = "outside target scope"
                return False
            if kind == "document" and not policy.include_documents:
                entry.status = "skipped_document"
                entry.skip_reason = "documents disabled by mirror policy"
                return False
            if kind == "asset" and not policy.include_assets:
                entry.status = "skipped_asset"
                entry.skip_reason = "assets disabled by mirror policy"
                return False
            if enforce_class_budget and page_class != "document" and selected_by_class.get(page_class, 0) >= page_class_budget(policy, page_class):
                entry.status = "skipped_class_budget"
                entry.skip_reason = f"{page_class} class budget exhausted"
                return False
            seen.add(url)
            selected.append(url)
            if page_class != "document":
                selected_by_class[page_class] = selected_by_class.get(page_class, 0) + 1
            return True

        include(home_url, enforce_class_budget=False)
        for raw in seed_urls or []:
            seed = normalize_url(raw)
            if seed == home_url:
                continue
            entry = crawl_index.get(seed)
            if entry is None:
                crawl_index[seed] = CrawlIndexEntry(
                    url=seed,
                    discovered_from=["follow_up:seed"],
                    depth=0,
                    kind=resource_kind(seed),  # type: ignore[arg-type]
                    page_class=classify_page(seed, home_url=home_url, policy=policy),
                )
            elif "follow_up:seed" not in entry.discovered_from:
                entry.discovered_from.append("follow_up:seed")
            include(seed, enforce_class_budget=False)
        document_count = 0
        ranked = {
            page_class: sorted(
                [
                    url for url in urls
                    if url != home_url and classify_page(url, home_url=home_url, policy=policy) == page_class
                ],
                key=lambda candidate: (-page_score(candidate, target.categories), len(urlparse(candidate).path), candidate),
            )
            for page_class in PAGE_CLASS_ORDER
        }
        for page_class in PAGE_CLASS_ORDER:
            if page_class == "homepage":
                continue
            for url in ranked[page_class]:
                if len([item for item in selected if resource_kind(item) != "document"]) >= policy.max_pages:
                    break
                if include(url):
                    if resource_kind(url) == "document":
                        document_count += 1
            if len([item for item in selected if resource_kind(item) != "document"]) >= policy.max_pages:
                break
        if policy.include_documents and document_count < policy.max_documents_per_target:
            document_urls = sorted(
                [url for url in urls if classify_page(url, home_url=home_url, policy=policy) == "document"],
                key=lambda candidate: (-page_score(candidate, target.categories), candidate),
            )
            for url in document_urls:
                if document_count >= policy.max_documents_per_target:
                    break
                if resource_kind(url) != "document" or url in seen:
                    continue
                if include(url):
                    document_count += 1
        return selected

    def _cache_path(self, workspace_root: Path, *, url: str, suffix: str) -> Path:
        import hashlib

        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return workspace_root / "output" / "cache" / key / suffix

    def _fetch_html(
        self,
        url: str,
        *,
        target_root: Path,
        workspace_root: Path,
        policy: MirrorPolicy,
        ordinal: int,
        budget: BrowserlessCallBudget,
        ignore_cache: bool = False,
    ) -> MirrorResource:
        cache_path = self._cache_path(workspace_root, url=url, suffix="smart_scrape.json")
        if policy.reuse_cache and not ignore_cache and cache_path.exists():
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            result = BrowserlessSmartScrapeResult.model_validate(raw)
            result.raw = raw
            cache_attempt = BrowserlessAttempt(endpoint="smart-scrape", status="skipped", detail="reused cached Browserless response")
            return self._materialize_html_result(url, result, target_root=target_root, ordinal=ordinal, extra_attempts=[cache_attempt])

        if not budget.can_spend():
            detail = f"Browserless call budget exhausted after {budget.used} call(s)"
            return MirrorResource(
                resource_id=f"resource-{ordinal:04d}",
                url=url,
                kind="html",
                status="skipped",
                attempts=[BrowserlessAttempt(endpoint="smart-scrape", status="skipped", detail=detail)],
                error_message=detail,
            )

        budget.spend()
        try:
            result = self.browserless.smart_scrape(url=url, timeout_ms=policy.scrape_timeout_ms)
        except Exception as exc:
            return MirrorResource(
                resource_id=f"resource-{ordinal:04d}",
                url=url,
                kind="html",
                status="failed",
                attempts=[BrowserlessAttempt(endpoint="smart-scrape", status="failed", detail=str(exc))],
                error_message=str(exc),
            )
        write_json(cache_path, result.raw or result.model_dump(by_alias=True))
        return self._materialize_html_result(url, result, target_root=target_root, ordinal=ordinal)

    def _materialize_html_result(
        self,
        url: str,
        result: BrowserlessSmartScrapeResult,
        *,
        target_root: Path,
        ordinal: int,
        extra_attempts: list[BrowserlessAttempt] | None = None,
    ) -> MirrorResource:
        attempts = list(extra_attempts or [])
        attempts.append(
            BrowserlessAttempt(
                endpoint="smart-scrape",
                status="succeeded" if result.ok else "failed",
                detail=result.message,
                strategy=result.strategy,
                attempted=result.attempted,
            )
        )
        html = result.content if isinstance(result.content, str) else ""
        text = text_from_markdown_or_html(result.markdown, html)
        base = f"{ordinal:04d}-{slugify(urlparse(url).path.strip('/') or 'home')}"
        html_path = target_root / "raw" / f"{base}.html"
        json_path = target_root / "json" / f"{base}.json"
        markdown_path = target_root / "markdown" / f"{base}.md"
        text_path = target_root / "text" / f"{base}.txt"
        sha = sha256_text(html or json.dumps(result.raw, sort_keys=True))
        local_metadata = {
            "url": url,
            "artifact_paths": {
                "html_path": str(html_path),
                "json_path": str(json_path),
                "markdown_path": str(markdown_path) if result.markdown else None,
                "text_path": str(text_path),
            },
            "sha256": sha,
            "captured_at_unix": int(time.time()),
        }
        if html:
            write_text(html_path, html)
        if result.markdown:
            write_text(markdown_path, result.markdown)
        write_text(text_path, text)
        write_json(json_path, json_safe_browserless_response(result.raw or result.model_dump(by_alias=True), local_metadata=local_metadata))
        return MirrorResource(
            resource_id=f"resource-{ordinal:04d}",
            url=url,
            final_url=url,
            kind="html",
            status="fetched" if result.ok and (html or text) else "failed",
            content_type=result.content_type,
            response_status_code=result.status_code,
            html_path=str(html_path) if html else None,
            json_path=str(json_path),
            markdown_path=str(markdown_path) if result.markdown else None,
            text_path=str(text_path),
            sha256=sha,
            text_chars=len(text),
            browserless_strategy=result.strategy,
            browserless_attempted=result.attempted,
            attempts=attempts,
            error_message=result.message if not result.ok else None,
        )

    def _fetch_document(self, url: str, *, target_root: Path, workspace_root: Path, ordinal: int) -> MirrorResource:
        client = self.static_client
        close_client = False
        if client is None:
            client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0))
            close_client = True
        try:
            response = client.get(url)
            content = getattr(response, "content", b"")
            status_code = getattr(response, "status_code", None)
            content_type = getattr(response, "headers", {}).get("content-type") if hasattr(response, "headers") else None
            if status_code is not None and status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")
            extension = path_extension(url) or ".bin"
            base = f"{ordinal:04d}-{slugify(urlparse(url).path.strip('/') or 'document')}{extension}"
            document_path = target_root / "documents" / base
            write_bytes(document_path, content)
            text_path: Path | None = None
            parsed_text = parse_pdf_text(document_path) if extension.lower() == ".pdf" else None
            if parsed_text:
                text_path = target_root / "text" / f"{Path(base).stem}.txt"
                write_text(text_path, parsed_text)
            return MirrorResource(
                resource_id=f"resource-{ordinal:04d}",
                url=url,
                final_url=str(getattr(response, "url", url)),
                kind="document",
                status="fetched",
                content_type=content_type,
                response_status_code=status_code,
                document_path=str(document_path),
                text_path=str(text_path) if text_path else None,
                sha256=sha256_bytes(content),
                text_chars=len(parsed_text or ""),
                attempts=[BrowserlessAttempt(endpoint="static-http", status="succeeded", detail="document downloaded")],
            )
        except Exception as exc:
            return MirrorResource(
                resource_id=f"resource-{ordinal:04d}",
                url=url,
                kind="document",
                status="failed",
                attempts=[BrowserlessAttempt(endpoint="static-http", status="failed", detail=str(exc))],
                error_message=str(exc),
            )
        finally:
            if close_client:
                client.close()

    def _links_from_resource(self, resource: MirrorResource) -> list[str]:
        if not resource.json_path:
            return []
        try:
            payload = json.loads(Path(resource.json_path).read_text(encoding="utf-8"))
        except Exception:
            return []
        links = payload.get("browserless", {}).get("links")
        return [link for link in links if isinstance(link, str)] if isinstance(links, list) else []

    def _quality_report(
        self,
        resources: list[MirrorResource],
        crawl_index: dict[str, CrawlIndexEntry],
        *,
        target: MirrorTarget,
        policy: MirrorPolicy,
        browserless_calls_used: int,
    ) -> QualityReport:
        fetched_pages = sum(1 for resource in resources if resource.kind == "html" and resource.status == "fetched")
        failed_pages = sum(1 for resource in resources if resource.kind == "html" and resource.status == "failed")
        fetched_documents = sum(1 for resource in resources if resource.kind == "document" and resource.status == "fetched")
        total_text_chars = sum(resource.text_chars for resource in resources)
        skipped = sum(1 for entry in crawl_index.values() if entry.status.startswith("skipped"))
        reasons: list[str] = []
        if fetched_pages == 0:
            reasons.append("no usable HTML pages were fetched")
        if total_text_chars < policy.min_text_chars:
            reasons.append(f"total text below threshold: {total_text_chars} < {policy.min_text_chars}")
        urls = [resource.final_url or resource.url for resource in resources if resource.status == "fetched"]
        has_product = any(any(token in url.lower() for token in PRODUCT_TOKENS) for url in urls)
        has_about = any(any(token in url.lower() for token in ABOUT_TOKENS) for url in urls)
        if policy.require_product_or_capability and not has_product:
            reasons.append("no product or capability page fetched")
        if policy.require_contact_or_about and not has_about:
            reasons.append("no about/contact page fetched")
        if failed_pages:
            reasons.append(f"{failed_pages} HTML page(s) failed")
        budget_exhausted = policy.max_browserless_calls_per_target is not None and browserless_calls_used >= policy.max_browserless_calls_per_target
        if budget_exhausted:
            reasons.append(f"Browserless call budget exhausted: {browserless_calls_used}/{policy.max_browserless_calls_per_target}")

        if fetched_pages == 0:
            status = "failed"
        elif reasons:
            status = "partial" if total_text_chars >= policy.min_text_chars else "review_required"
        else:
            status = "complete"
        return QualityReport(
            status=status,  # type: ignore[arg-type]
            fetched_pages=fetched_pages,
            failed_pages=failed_pages,
            fetched_documents=fetched_documents,
            discovered_urls=len(crawl_index),
            skipped_urls=skipped,
            total_text_chars=total_text_chars,
            browserless_calls_used=browserless_calls_used,
            browserless_call_budget=policy.max_browserless_calls_per_target,
            budget_exhausted=budget_exhausted,
            reasons=reasons,
        )
