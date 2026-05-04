"""End-of-pipeline tagline generation.

A single-prompt Haiku pass that produces a ≤100-word editorial tagline
per vendor, given the structured Profile, ProductCatalog, and
homepage text. Bypasses the Claude Agent SDK because there's no
tool-using or multi-turn behaviour to orchestrate — one prompt in,
one string out. Direct `anthropic.Messages.create` is the right
primitive.

The tagline is editorial, not evidence-bearing: no citations, no
verbatim guarantee. It exists to give FE vendor cards a one-glance
summary that the structured fields alone can't convey.

Failures here are isolated — a tagline error must NOT block the
upstream extract. The runner journals the error and moves on; the
profile remains usable without a tagline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import anthropic

from .corpus import CorpusReader
from .schema import Profile, ProductCatalog


DEFAULT_TAGLINE_MODEL = "claude-haiku-4-5"
TAGLINE_MAX_WORDS = 100
DEFAULT_TIMEOUT_SEC = 60.0
HOMEPAGE_TEXT_CHAR_CAP = 8000


def _homepage_text(corpus: CorpusReader) -> str:
    """Return the homepage's extracted text, or "" if unavailable.
    Falls back to resource-0001 (which is always the homepage by
    canonical convention)."""
    for r in corpus.fetched_resources():
        if r.page_class == "homepage" and r.text_path is not None:
            return r.text_path.read_text()
    first = corpus.resource_by_id("resource-0001")
    if first is not None and first.text_path is not None:
        return first.text_path.read_text()
    return ""


def _profile_summary(profile: Profile) -> str:
    """Compact human-readable digest of the structured profile."""
    role = profile.drone_supply_chain_role.value or "unknown"
    cats_primary = [c.category for c in profile.products_categories.items if c.is_primary]
    primary = cats_primary[0] if cats_primary else "(none)"
    secondary = [c.category for c in profile.products_categories.items if not c.is_primary]

    hq = profile.headquarters.value
    hq_str = "unknown"
    if hq is not None:
        parts = [p for p in (hq.city, hq.state_or_province, hq.country) if p]
        hq_str = ", ".join(parts) if parts else "unknown"

    products_listed = [p.name for p in profile.products.items[:8]]
    products_str = ", ".join(products_listed) if products_listed else "(none enumerated)"

    return (
        f"  display_name: {profile.display_name}\n"
        f"  role: {role}\n"
        f"  primary category: {primary}"
        + (f" (also: {', '.join(secondary)})" if secondary else "")
        + "\n"
        f"  hq: {hq_str}\n"
        f"  ndaa: {profile.ndaa.value or 'unknown'}\n"
        f"  blue_uas: {profile.blue_uas.value or 'unknown'}\n"
        f"  readiness: {profile.readiness.value or 'unknown'}\n"
        f"  vendor-pass-named products: {products_str}\n"
    )


def _products_summary(catalog: ProductCatalog | None) -> str:
    if catalog is None or not catalog.products:
        return "  (none)"
    lines: list[str] = []
    for p in catalog.products[:20]:
        bits = [
            f"{p.name}",
            f"({p.category}/{p.granularity}/{p.readiness})",
            f"— {p.descriptor}",
        ]
        compliance = []
        if p.ndaa not in ("unknown", None):
            compliance.append(f"NDAA={p.ndaa}")
        if p.blue_uas not in ("unknown", None):
            compliance.append(f"BlueUAS={p.blue_uas}")
        if compliance:
            bits.append(f"[{', '.join(compliance)}]")
        lines.append("  - " + " ".join(bits))
    if len(catalog.products) > 20:
        lines.append(f"  ... and {len(catalog.products) - 20} more")
    return "\n".join(lines)


def build_prompt(
    profile: Profile,
    catalog: ProductCatalog | None,
    homepage_text: str,
) -> str:
    truncated_homepage = homepage_text[:HOMEPAGE_TEXT_CHAR_CAP]
    return (
        f"Write a single tagline (no more than {TAGLINE_MAX_WORDS} words) "
        f"for {profile.display_name}.\n"
        f"\n"
        f"Audience: a curated reference for the US/allied unmanned-systems "
        f"supplier base. Readers are program managers, primes' "
        f"supply-chain teams, and policy researchers who want to know in "
        f"one or two sentences what this vendor does, what makes them "
        f"notable, and what gaps exist in the public evidence.\n"
        f"\n"
        f"Style: terse, factual, no marketing fluff. Lead with what they "
        f"make. If a notable certification status (NDAA, Blue UAS) or "
        f"contract/program is in the structured data, mention it. If the "
        f"public evidence is thin or contradictory, say so plainly.\n"
        f"\n"
        f"Don't repeat the company name. Don't start with \"Vendor:\" or "
        f"any label. Don't use bullet points. Output only the tagline "
        f"prose, nothing else.\n"
        f"\n"
        f"STRUCTURED PROFILE:\n"
        f"{_profile_summary(profile)}\n"
        f"PRODUCTS:\n"
        f"{_products_summary(catalog)}\n"
        f"\n"
        f"HOMEPAGE TEXT (truncated to {HOMEPAGE_TEXT_CHAR_CAP} chars):\n"
        f"{truncated_homepage}\n"
    )


def _truncate_to_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",.;:") + "…"


async def generate_tagline(
    *,
    profile: Profile,
    catalog: ProductCatalog | None,
    corpus: CorpusReader,
    model: str = DEFAULT_TAGLINE_MODEL,
    timeout_sec: float | None = DEFAULT_TIMEOUT_SEC,
    client: anthropic.AsyncAnthropic | None = None,
) -> str:
    """Generate a tagline. Returns the trimmed string. Raises on
    network/auth failures so the caller can decide whether to swallow."""
    if client is None:
        client = anthropic.AsyncAnthropic()
    homepage = _homepage_text(corpus)
    prompt = build_prompt(profile, catalog, homepage)

    async def _call() -> str:
        msg = await client.messages.create(
            model=model,
            max_tokens=300,  # 100 words ~= 130 tokens; cap with slack
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            getattr(b, "text", "") for b in msg.content
        ).strip()
        return _truncate_to_words(text, TAGLINE_MAX_WORDS)

    if timeout_sec is None:
        return await _call()
    return await asyncio.wait_for(_call(), timeout=timeout_sec)


def write_tagline_into_profile(profile_path: Path, tagline: str) -> None:
    """Update profile.json in place with the new tagline. Loads, sets
    `tagline`, re-validates, writes. Re-validation guards against
    schema drift between when the profile was extracted and now."""
    import json
    doc = json.loads(profile_path.read_text())
    doc["tagline"] = tagline
    # Re-validate to ensure the doc is still well-formed.
    Profile.model_validate(doc)
    profile_path.write_text(json.dumps(doc, indent=2))
