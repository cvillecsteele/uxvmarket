"""Adapter for canonicalized `vendors/<slug>/{profile,products}.json` data."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from generator import designations

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VENDORS_ROOT = PROJECT_ROOT / "vendors"

COUNTRY_ISO2: dict[str, str] = {
    "Australia": "AU",
    "Austria": "AT",
    "Belgium": "BE",
    "Canada": "CA",
    "Croatia": "HR",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Germany": "DE",
    "India": "IN",
    "Netherlands": "NL",
    "Norway": "NO",
    "Poland": "PL",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "Sweden": "SE",
    "Switzerland": "CH",
    "United Kingdom": "GB",
    "United States": "US",
    "US": "US",
}

COUNTRY_NAMES: dict[str, str] = {
    "US": "United States",
    "GB": "United Kingdom",
}

CATEGORY_SLUGS: dict[str, str] = {
    "propulsion_electronics": "propulsion-electronics",
    "propulsion_mechanical": "propulsion-mechanical",
    "power_systems": "power-systems",
    "flight_and_vehicle_control": "flight-vehicle-control",
    "sensors_and_navigation": "sensors-navigation",
    "isr_payloads": "isr-payloads",
    "electronic_warfare": "electronic-warfare",
    "munitions": "munitions",
    "communications": "communications",
    "mechanical_subsystems": "mechanical-subsystems",
    "structures_and_materials": "structures-materials",
    "airframes": "airframes",
    "recovery_systems": "recovery-systems",
    "flight_termination": "flight-termination",
    "ground_segment": "ground-segment",
    "test_and_measurement": "test-measurement",
}

CATEGORY_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "slug": "propulsion-electronics",
        "display_name": "Propulsion Electronics",
        "is_meta": False,
        "description": "Electronic controls, motor drives, ESCs, inverters, and power electronics for unmanned propulsion systems.",
    },
    {
        "slug": "propulsion-mechanical",
        "display_name": "Propulsion Mechanical",
        "is_meta": False,
        "description": "Engines, motors, propellers, rotors, turbines, and mechanical propulsion hardware for UxV platforms.",
    },
    {
        "slug": "power-systems",
        "display_name": "Power Systems",
        "is_meta": False,
        "description": "Cells, packs, fuel systems, generators, chargers, converters, and onboard power distribution.",
    },
    {
        "slug": "flight-vehicle-control",
        "display_name": "Flight and Vehicle Control",
        "is_meta": False,
        "description": "Autopilots, actuators, flight computers, vehicle-control software, and control-surface subsystems.",
    },
    {
        "slug": "sensors-navigation",
        "display_name": "Sensors and Navigation",
        "is_meta": False,
        "description": "GNSS, INS, IMU, radar, lidar, air-data, perception, and navigation-aiding components.",
    },
    {
        "slug": "isr-payloads",
        "display_name": "ISR Payloads",
        "is_meta": False,
        "description": "Imaging, sensing, mapping, targeting, and mission payload systems for unmanned platforms.",
    },
    {
        "slug": "electronic-warfare",
        "display_name": "Electronic Warfare",
        "is_meta": False,
        "description": "EW, RF detection, jamming, spoofing, counter-UAS, and spectrum-effects subsystems.",
    },
    {
        "slug": "munitions",
        "display_name": "Munitions",
        "is_meta": False,
        "description": "Payload-release, effects delivery, loitering-munition, and munition-adjacent subsystems.",
    },
    {
        "slug": "communications",
        "display_name": "Communications",
        "is_meta": False,
        "description": "Radios, datalinks, antennas, SATCOM, mesh networking, C2 links, and telemetry hardware.",
    },
    {
        "slug": "mechanical-subsystems",
        "display_name": "Mechanical Subsystems",
        "is_meta": False,
        "description": "Actuation, deployment, launch, landing, enclosure, connector, and other mechanical subsystems.",
    },
    {
        "slug": "structures-materials",
        "display_name": "Structures and Materials",
        "is_meta": False,
        "description": "Composites, radomes, coatings, airframe materials, structural components, and fabrication services.",
    },
    {
        "slug": "airframes",
        "display_name": "Airframes",
        "is_meta": False,
        "description": "Complete aircraft, UAV platforms, target drones, and airframe-level unmanned systems.",
    },
    {
        "slug": "recovery-systems",
        "display_name": "Recovery Systems",
        "is_meta": False,
        "description": "Parachutes, recovery controllers, airbags, termination-linked recovery, and landing safety systems.",
    },
    {
        "slug": "flight-termination",
        "display_name": "Flight Termination",
        "is_meta": False,
        "description": "Flight termination systems, range safety components, destruct systems, and command receivers.",
    },
    {
        "slug": "ground-segment",
        "display_name": "Ground Segment",
        "is_meta": False,
        "description": "Ground control, launch/recovery, tracking, operator stations, test stations, and mission infrastructure.",
    },
    {
        "slug": "test-measurement",
        "display_name": "Test and Measurement",
        "is_meta": False,
        "description": "Qualification, environmental, EMC, battery, propulsion, simulation, and validation test capabilities.",
    },
)


def has_canonical_source(vendors_root: Path = DEFAULT_VENDORS_ROOT) -> bool:
    return vendors_root.exists() and any(vendors_root.glob("*/profile.json"))


def all_categories() -> list[dict[str, Any]]:
    return list(CATEGORY_DEFINITIONS)


def load_vendors(vendors_root: Path, desig_state: dict[str, str]) -> list[dict[str, Any]]:
    vendors: list[dict[str, Any]] = []
    used_slugs: set[str] = set()
    for vendor_dir in sorted(path for path in vendors_root.iterdir() if path.is_dir()):
        profile_path = vendor_dir / "profile.json"
        products_path = vendor_dir / "products.json"
        if not profile_path.exists() or not products_path.exists():
            continue
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        products_doc = json.loads(products_path.read_text(encoding="utf-8"))
        vendor = _vendor_from_docs(vendor_dir, profile, products_doc, desig_state)
        slug = vendor["slug"]
        if slug in used_slugs:
            slug = f"{slug}-{vendor['primary_category']}"
            vendor["slug"] = slug
        used_slugs.add(slug)
        vendors.append(vendor)
    return vendors


def _vendor_from_docs(
    vendor_dir: Path,
    profile: dict[str, Any],
    products_doc: dict[str, Any],
    desig_state: dict[str, str],
) -> dict[str, Any]:
    slug = _slug(profile.get("target_id") or vendor_dir.name)
    products = [_product(item) for item in products_doc.get("products", []) if item.get("name")]
    categories = _categories(profile, products)
    primary_category = categories[0] if categories else "mechanical-subsystems"
    reviewed_at = _reviewed_at(vendor_dir, profile, products_doc)
    country_iso2, country_name = _country(profile)
    hq = _field(profile, "headquarters")
    role = _field(profile, "drone_supply_chain_role")
    compliance = _compliance(profile)
    readiness = _readiness(profile, products)
    tagline = profile.get("tagline") or _fallback_tagline(profile, products)

    return {
        "slug": slug,
        "designation": designations.assign(slug, primary_category, desig_state),
        "canonical_name": profile.get("display_name") or products_doc.get("display_name") or slug,
        "homepage_url": profile.get("homepage_url") or products_doc.get("homepage_url"),
        "hq_city": hq.get("city") if isinstance(hq, dict) else None,
        "hq_region": hq.get("state_or_province") if isinstance(hq, dict) else None,
        "country_iso2": country_iso2,
        "country_name": country_name,
        "primary_category": primary_category,
        "categories": categories,
        "compliance_posture": compliance,
        "readiness_posture": readiness,
        "ownership_type": role.replace("_", " ") if role else None,
        "product_lines": [product["name"] for product in products[:12]],
        "reviewed_at": reviewed_at,
        "tagline": tagline,
        "facts": _facts(profile, products, role, compliance, readiness, country_name),
        "products": products,
    }


def _field(doc: dict[str, Any], key: str) -> Any:
    value = doc.get(key)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _evidence(doc: dict[str, Any], key: str) -> dict[str, str] | None:
    value = doc.get(key)
    if not isinstance(value, dict):
        return None
    evidence = value.get("evidence") or []
    if not evidence:
        return None
    item = evidence[0]
    row: dict[str, str] = {"ref": "SUPPORTING EVIDENCE"}
    if item.get("snippet"):
        row["snippet"] = item["snippet"]
    if item.get("url"):
        row["source_url"] = item["url"]
    if item.get("page_class"):
        row["renderer"] = item["page_class"]
    return row


def _fact(label: str, value: str | None, evidence: dict[str, str] | None = None) -> dict[str, str]:
    row = {"label": label}
    if value:
        row["value"] = value
    if evidence:
        row.update(evidence)
    return row


def _facts(
    profile: dict[str, Any],
    products: list[dict[str, Any]],
    role: str | None,
    compliance: str,
    readiness: str,
    country_name: str,
) -> list[dict[str, str]]:
    hq = _field(profile, "headquarters")
    hq_parts: list[str] = []
    if isinstance(hq, dict):
        for key in ("city", "state_or_province"):
            if hq.get(key):
                hq_parts.append(str(hq[key]))
    hq_parts.append(country_name)

    facts = [
        _fact("Headquarters", ", ".join(dict.fromkeys(hq_parts)), _evidence(profile, "headquarters")),
        _fact("Supply-chain role", role.replace("_", " ").title() if role else None, _evidence(profile, "drone_supply_chain_role")),
        _fact("Compliance", _compliance_label(compliance), _evidence(profile, "ndaa")),
        _fact("Readiness", _readiness_label(readiness), _evidence(profile, "readiness")),
    ]
    if products:
        facts.append(_fact("Product count", str(len(products))))
        top_categories = Counter(product["category"] for product in products).most_common(4)
        facts.append(_fact("Product categories", ", ".join(_category_label(category) for category, _ in top_categories)))
    return facts


def _product(item: dict[str, Any]) -> dict[str, Any]:
    evidence = item.get("evidence") or []
    first_evidence = evidence[0] if evidence else {}
    product = {
        "name": item.get("name"),
        "category": _normalize_category(item.get("category")) or "mechanical-subsystems",
        "descriptor": item.get("descriptor"),
        "granularity": item.get("granularity"),
        "readiness": _normalize_readiness(item.get("readiness")),
        "ndaa": item.get("ndaa") or "unknown",
        "blue_uas": item.get("blue_uas") or "unknown",
        "notes": item.get("notes"),
        "source_url": first_evidence.get("url"),
        "snippet": first_evidence.get("snippet"),
    }
    return {key: value for key, value in product.items() if value is not None}


def _categories(profile: dict[str, Any], products: list[dict[str, Any]]) -> list[str]:
    ordered: list[str] = []
    primary: str | None = None
    for item in ((profile.get("products_categories") or {}).get("items") or []):
        category = _normalize_category(item.get("category"))
        if not category:
            continue
        if item.get("is_primary") and primary is None:
            primary = category
        if category not in ordered:
            ordered.append(category)
    for category, _ in Counter(product["category"] for product in products).most_common():
        if category not in ordered:
            ordered.append(category)
    if primary and primary in ordered:
        ordered.remove(primary)
        ordered.insert(0, primary)
    return ordered


def _country(profile: dict[str, Any]) -> tuple[str, str]:
    hq = _field(profile, "headquarters")
    raw = hq.get("country") if isinstance(hq, dict) else None
    iso2 = COUNTRY_ISO2.get(str(raw), str(raw or "US").upper()[:2])
    name = COUNTRY_NAMES.get(iso2) or (raw if raw and len(str(raw)) > 2 else None) or iso2
    return iso2, str(name)


def _compliance(profile: dict[str, Any]) -> str:
    blue = _field(profile, "blue_uas")
    ndaa = _field(profile, "ndaa")
    if blue == "yes":
        return "blue_uas_framework"
    if ndaa == "yes":
        return "ndaa_compliant"
    if ndaa == "no":
        return "not_eligible"
    return "unknown"


def _readiness(profile: dict[str, Any], products: list[dict[str, Any]]) -> str:
    value = _normalize_readiness(_field(profile, "readiness"))
    if value != "unknown":
        return value
    product_values = [product["readiness"] for product in products]
    if "production" in product_values:
        return "production"
    if "low_rate" in product_values:
        return "low_rate"
    if "prototype" in product_values:
        return "prototype"
    if "engineering_services" in product_values:
        return "engineering_services"
    return "unknown"


def _reviewed_at(vendor_dir: Path, profile: dict[str, Any], products_doc: dict[str, Any]) -> str:
    report_path = vendor_dir / "canonicalize_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("ts"):
            return str(report["ts"])[:10]
    for doc in (profile, products_doc):
        created_at = (doc.get("meta") or {}).get("created_at")
        if created_at:
            return str(created_at)[:10]
    return "2026-05-03"


def _fallback_tagline(profile: dict[str, Any], products: list[dict[str, Any]]) -> str:
    role = _field(profile, "drone_supply_chain_role")
    if products:
        names = ", ".join(product["name"] for product in products[:3])
        return f"{role.replace('_', ' ').title() if role else 'Vendor'} with public evidence for {names}."
    return "(tagline pending review)"


def _normalize_category(value: str | None) -> str | None:
    if not value:
        return None
    return CATEGORY_SLUGS.get(value, value.replace("_", "-"))


def _normalize_readiness(value: str | None) -> str:
    if value in {None, "unknown"}:
        return "unknown"
    if value in {"low_rate", "low_rate_production"}:
        return "low_rate"
    if value == "engineering_services":
        return "engineering_services"
    if value in {"production", "prototype"}:
        return value
    return "unknown"


def _category_label(slug: str) -> str:
    return slug.replace("-", " ").title()


def _compliance_label(value: str) -> str:
    return {
        "blue_uas_framework": "Blue UAS Framework",
        "ndaa_compliant": "NDAA-compliant",
        "not_eligible": "Not NDAA-eligible",
        "unknown": "Compliance unknown",
    }.get(value, "Compliance unknown")


def _readiness_label(value: str) -> str:
    return {
        "production": "Production",
        "low_rate": "Low-rate production",
        "prototype": "Prototype",
        "engineering_services": "Engineering services",
        "unknown": "Readiness unknown",
    }.get(value, "Readiness unknown")


def _slug(value: str) -> str:
    return value.strip().lower().replace("_", "-")
