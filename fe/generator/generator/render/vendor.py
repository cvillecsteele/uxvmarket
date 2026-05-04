"""Render one vendor as Markdown with YAML frontmatter for fe/site."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_COMPLIANCE_MAP = {
    "blue_uas_framework": "blue-uas-framework",
    "blue_uas_cleared": "blue-uas-cleared",
    "ndaa_compliant": "ndaa-compliant",
    "not_eligible": "not-eligible",
    "unknown": "unknown",
    None: "unknown",
}

_READINESS_MAP = {
    "production": "production",
    "low_rate": "low-rate-production",
    "prototype": "prototype",
    "engineering_services": "engineering-services",
    "unknown": "unknown",
    None: "unknown",
}

_VALID_DOMAINS = {"uav", "ugv", "usv", "uuv"}


def _normalize_slug(db_slug: str | None) -> str | None:
    if not db_slug:
        return None
    return db_slug.replace("_", "-")


def _normalize_domains(domain_slugs: list[str]) -> list[str]:
    return [d for d in domain_slugs if d in _VALID_DOMAINS]


def render(vendor_data: dict[str, Any], out_dir: Path) -> Path:
    """Write `<slug>.md` to `out_dir`. Idempotent."""
    slug = vendor_data["slug"]
    out_path = out_dir / f"{slug}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter: dict[str, Any] = {
        "designation": vendor_data["designation"],
        "canonical_name": vendor_data["canonical_name"],
        "legal_name": vendor_data.get("legal_name"),
        "homepage_url": vendor_data.get("homepage_url"),
        "hq_city": vendor_data.get("hq_city"),
        "hq_region": vendor_data.get("hq_region"),
        "country_iso2": vendor_data.get("country_iso2"),
        "country_name": vendor_data.get("country_name"),
        "primary_category": _normalize_slug(vendor_data.get("primary_category")),
        "categories": [
            normalized
            for normalized in (
                _normalize_slug(slug) for slug in vendor_data.get("categories", [])
            )
            if normalized
        ],
        "cross_listings": vendor_data.get("cross_listings", []),
        "compliance_posture": _COMPLIANCE_MAP.get(vendor_data.get("compliance_posture"), "unknown"),
        "readiness_posture": _READINESS_MAP.get(vendor_data.get("readiness_posture"), "unknown"),
        "certifications": vendor_data.get("certifications", []),
        "domains": _normalize_domains(vendor_data.get("domains", [])),
        "founded_year": vendor_data.get("founded_year"),
        "ownership_type": vendor_data.get("ownership_type"),
        "employee_count": vendor_data.get("employee_count"),
        "product_lines": vendor_data.get("product_lines", []),
        "products": vendor_data.get("products", []),
        "reviewed_at": vendor_data["reviewed_at"],
        "tagline": vendor_data.get("tagline") or "(tagline pending review)",
        "facts": vendor_data.get("facts", []),
    }

    cleaned: dict[str, Any] = {}
    for key, value in frontmatter.items():
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        cleaned[key] = value

    body = "---\n" + yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True, width=120) + "---\n"
    out_path.write_text(body, encoding="utf-8")
    return out_path
