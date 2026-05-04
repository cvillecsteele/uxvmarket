from __future__ import annotations

import pytest
from pydantic import ValidationError

from uxv_extract.schema import (
    Answer,
    CategoryClaim,
    Citation,
    FetchRequest,
    Headquarters,
    ListAnswer,
    ProductCatalog,
    ProductCatalogSubmission,
    ProductDetail,
    ProductMention,
    Profile,
    ProfileMeta,
    ProfileSubmission,
)


def _good_citation(**overrides: object) -> Citation:
    base = dict(
        source_kind="mirror",
        resource_id="resource-0001",
        line_start=1,
        line_end=2,
        url="https://example.com/",
        page_class="homepage",
        snippet=(
            "We design and manufacture heavy-lift drones in Florida for "
            "federal customers and first responders."
        ),
    )
    base.update(overrides)
    return Citation(**base)  # type: ignore[arg-type]


def _good_role_answer(**overrides: object) -> Answer:
    base = dict(
        value="oem",
        confidence="high",
        status="answered",
        evidence=[_good_citation()],
        notes=None,
    )
    base.update(overrides)
    return Answer(**base)  # type: ignore[arg-type]


def _unknown_answer(**overrides: object) -> Answer:
    base = dict(
        value=None,
        confidence="low",
        status="unknown",
        evidence=[],
        notes=None,
    )
    base.update(overrides)
    return Answer(**base)  # type: ignore[arg-type]


def _unknown_list_answer(**overrides: object) -> ListAnswer:
    base = dict(
        items=[],
        confidence="low",
        status="unknown",
        notes=None,
    )
    base.update(overrides)
    return ListAnswer(**base)  # type: ignore[arg-type]


# -- Citation --------------------------------------------------------------


def test_citation_accepts_valid_line_range() -> None:
    c = _good_citation()
    assert c.source_kind == "mirror"
    assert c.resource_id == "resource-0001"
    assert c.line_start == 1
    assert c.line_end == 2


def test_citation_rejects_inverted_line_range() -> None:
    with pytest.raises(ValidationError, match="line_end"):
        _good_citation(line_start=5, line_end=3)


def test_citation_rejects_zero_line_start() -> None:
    with pytest.raises(ValidationError):
        _good_citation(line_start=0, line_end=2)


def test_citation_accepts_single_line_range() -> None:
    c = _good_citation(line_start=3, line_end=3)
    assert c.line_start == c.line_end == 3


def test_citation_snippet_is_optional_at_construction() -> None:
    """The agent never types snippet text — the runner hydrates it from
    the file. Schema accepts an empty default."""
    c = _good_citation(snippet="")
    assert c.snippet == ""


def test_citation_rejects_unknown_source_kind() -> None:
    with pytest.raises(ValidationError):
        _good_citation(source_kind="random_blog")


# -- Answer ----------------------------------------------------------------


def test_answered_status_requires_citation() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        _good_role_answer(evidence=[])


def test_unknown_status_allows_empty_evidence() -> None:
    a = _unknown_answer()
    assert a.status == "unknown"


def test_needs_more_fetches_status_allows_empty_evidence() -> None:
    a = _good_role_answer(
        value=None, status="needs_more_fetches", confidence="low", evidence=[]
    )
    assert a.status == "needs_more_fetches"


def test_role_enum_rejects_garbage_via_profile() -> None:
    with pytest.raises(ValidationError):
        _good_profile(
            drone_supply_chain_role=_good_role_answer(value="totally_made_up_role")
        )


def test_role_enum_accepts_all_known_roles_via_profile() -> None:
    for role in [
        "oem",
        "subsystem_supplier",
        "component_supplier",
        "software_platform",
        "service_provider",
        "reseller",
        "integrator",
        "broad_industrial",
        "none",
    ]:
        _good_profile(drone_supply_chain_role=_good_role_answer(value=role))


# -- FetchRequest ----------------------------------------------------------


def test_fetch_request_basic() -> None:
    fr = FetchRequest(
        url="https://example.com/products",
        reason="No product page in corpus; need it to confirm OEM vs reseller.",
        expected_evidence=["drone_supply_chain_role"],
        source_hint="skipped_class_budget in crawl_index",
        in_corpus_index=True,
    )
    assert fr.url == "https://example.com/products"


def test_fetch_request_defaults_for_unindexed_url() -> None:
    fr = FetchRequest(
        url="https://example.com/legal/terms",
        reason="Legal page may reveal entity jurisdiction.",
        expected_evidence=["drone_supply_chain_role"],
    )
    assert fr.in_corpus_index is False
    assert fr.source_hint is None


def test_fetch_request_rejects_empty_expected_evidence() -> None:
    """A fetch request with no expected_evidence is meaningless — what
    field would it help?"""
    with pytest.raises(ValidationError, match="at least one"):
        FetchRequest(
            url="https://example.com/x",
            reason="something",
            expected_evidence=[],
        )


def test_fetch_request_rejects_unknown_field_name() -> None:
    """Typos and alternate field names slipping into expected_evidence
    would silently break the followups → mirror loop."""
    with pytest.raises(ValidationError, match="unknown field name"):
        FetchRequest(
            url="https://example.com/x",
            reason="bad name",
            expected_evidence=["product"],  # singular — typo for "products"
        )
    with pytest.raises(ValidationError, match="unknown field name"):
        FetchRequest(
            url="https://example.com/x",
            reason="bad name",
            expected_evidence=["drone_role"],
        )


# -- Headquarters ----------------------------------------------------------


def test_headquarters_minimal() -> None:
    hq = Headquarters(country="United States")
    assert hq.country == "United States"
    assert hq.city is None
    assert hq.state_or_province is None
    assert hq.address is None


def test_headquarters_full() -> None:
    hq = Headquarters(
        city="Apopka",
        state_or_province="Florida",
        country="United States",
        address="2375 W Orange Blossom Trail, Apopka, FL 32712",
    )
    assert hq.city == "Apopka"


def test_headquarters_country_required() -> None:
    with pytest.raises(ValidationError):
        Headquarters()  # type: ignore[call-arg]


# -- CategoryClaim ---------------------------------------------------------


def test_category_claim_basic() -> None:
    c = CategoryClaim(
        category="propulsion_electronics",
        is_primary=True,
        confidence="high",
        evidence=[_good_citation()],
        notes=None,
    )
    assert c.category == "propulsion_electronics"
    assert c.is_primary is True


def test_category_claim_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        CategoryClaim(
            category="propulsion_general",
            is_primary=True,
            confidence="high",
            evidence=[_good_citation()],
        )


def test_category_claim_accepts_all_16_categories() -> None:
    for cat in [
        "airframes",
        "propulsion_electronics",
        "propulsion_mechanical",
        "power_systems",
        "flight_and_vehicle_control",
        "sensors_and_navigation",
        "isr_payloads",
        "electronic_warfare",
        "munitions",
        "communications",
        "mechanical_subsystems",
        "structures_and_materials",
        "recovery_systems",
        "flight_termination",
        "ground_segment",
        "test_and_measurement",
    ]:
        CategoryClaim(
            category=cat,
            is_primary=False,
            confidence="medium",
            evidence=[_good_citation()],
        )


# -- ProductMention --------------------------------------------------------


def test_product_mention_basic() -> None:
    p = ProductMention(
        name="Carrier H6 Hybrid",
        product_type="hexacopter UAV",
        confidence="high",
        evidence=[_good_citation()],
        notes=None,
    )
    assert p.name == "Carrier H6 Hybrid"
    assert p.product_type == "hexacopter UAV"


def test_product_mention_optional_type() -> None:
    p = ProductMention(
        name="RG-380E",
        product_type=None,
        confidence="medium",
        evidence=[_good_citation()],
    )
    assert p.product_type is None


def test_product_mention_rejects_empty_evidence() -> None:
    """Naming a product is an assertion — must be cited."""
    with pytest.raises(ValidationError, match="at least one Citation"):
        ProductMention(
            name="Phantom Product",
            product_type=None,
            confidence="medium",
            evidence=[],
        )


def test_category_claim_rejects_empty_evidence() -> None:
    """Listing a category is an assertion — must be cited."""
    with pytest.raises(ValidationError, match="at least one Citation"):
        CategoryClaim(
            category="airframes",
            is_primary=True,
            confidence="high",
            evidence=[],
        )


def test_product_detail_rejects_empty_evidence() -> None:
    """A ProductDetail is an assertion — must be cited."""
    with pytest.raises(ValidationError, match="at least one Citation"):
        _good_product(evidence=[])


# -- ListAnswer ------------------------------------------------------------


def test_list_answer_unknown() -> None:
    la = _unknown_list_answer()
    assert la.items == []
    assert la.status == "unknown"


def test_list_answer_answered_with_items() -> None:
    la = ListAnswer[CategoryClaim](
        items=[
            CategoryClaim(
                category="airframes" if False else "structures_and_materials",
                is_primary=True,
                confidence="high",
                evidence=[_good_citation()],
            )
        ],
        confidence="high",
        status="answered",
        notes=None,
    )
    assert len(la.items) == 1


def test_list_answer_answered_status_requires_items() -> None:
    """A status of 'answered' on a ListAnswer means the list is the agent's
    enumeration; an empty list contradicts that. Use 'not_disclosed' or
    'unknown' instead."""
    with pytest.raises(ValidationError, match="items"):
        ListAnswer[CategoryClaim](
            items=[], confidence="high", status="answered", notes=None
        )


# -- products_categories: primary-uniqueness rule --------------------------


def test_products_categories_exactly_one_primary_when_answered() -> None:
    two_primary = ListAnswer[CategoryClaim](
        items=[
            CategoryClaim(
                category="propulsion_electronics",
                is_primary=True,
                confidence="high",
                evidence=[_good_citation()],
            ),
            CategoryClaim(
                category="power_systems",
                is_primary=True,
                confidence="medium",
                evidence=[_good_citation()],
            ),
        ],
        confidence="high",
        status="answered",
        notes=None,
    )
    with pytest.raises(ValidationError, match="primary"):
        _good_profile(products_categories=two_primary)


def test_products_categories_zero_primary_when_answered_is_invalid() -> None:
    no_primary = ListAnswer[CategoryClaim](
        items=[
            CategoryClaim(
                category="propulsion_electronics",
                is_primary=False,
                confidence="high",
                evidence=[_good_citation()],
            )
        ],
        confidence="high",
        status="answered",
        notes=None,
    )
    with pytest.raises(ValidationError, match="primary"):
        _good_profile(products_categories=no_primary)


# -- Profile ---------------------------------------------------------------


def _good_profile(**overrides: object) -> Profile:
    base = dict(
        target_id="harris-aerial",
        run_id="test-run",
        display_name="Harris Aerial",
        homepage_url="https://harrisaerial.com",
        corpus_root="/abs/path",
        products_categories=_unknown_list_answer(),
        headquarters=_unknown_answer(),
        drone_supply_chain_role=_good_role_answer(),
        products=_unknown_list_answer(),
        ndaa=_unknown_answer(),
        blue_uas=_unknown_answer(),
        readiness=_unknown_answer(),
        unresolved_questions=[],
        fetch_requests=[],
        status="complete",
        meta=ProfileMeta(
            model="claude-sonnet-4-6",
            num_turns=4,
            total_cost_usd=0.12,
            created_at="2026-05-01T12:00:00+00:00",
            extract_version="0.1.0",
        ),
    )
    base.update(overrides)
    return Profile(**base)  # type: ignore[arg-type]


def test_profile_round_trips() -> None:
    p = _good_profile()
    j = p.model_dump_json()
    p2 = Profile.model_validate_json(j)
    assert p2 == p


def test_profile_needs_more_fetches_requires_fetch_request_for_field() -> None:
    answer = _good_role_answer(
        value=None, status="needs_more_fetches", confidence="low", evidence=[]
    )
    with pytest.raises(ValidationError, match="fetch_request"):
        _good_profile(
            drone_supply_chain_role=answer,
            fetch_requests=[],
            status="needs_more_fetches",
        )


def test_profile_needs_more_fetches_passes_with_matching_request() -> None:
    answer = _good_role_answer(
        value=None, status="needs_more_fetches", confidence="low", evidence=[]
    )
    fr = FetchRequest(
        url="https://harrisaerial.com/products",
        reason="need product page",
        expected_evidence=["drone_supply_chain_role"],
    )
    _good_profile(
        drone_supply_chain_role=answer,
        fetch_requests=[fr],
        status="needs_more_fetches",
    )


def test_profile_status_enum() -> None:
    with pytest.raises(ValidationError):
        _good_profile(status="finished")


# -- Generic per-field needs_more_fetches ----------------------------------


def test_needs_more_fetches_validator_applies_to_ndaa() -> None:
    """Each Answer-typed field gets the same validation: if its status is
    needs_more_fetches, a fetch_request must list that field name."""
    ndaa_unknown = _unknown_answer(status="needs_more_fetches")
    with pytest.raises(ValidationError, match="ndaa"):
        _good_profile(ndaa=ndaa_unknown)


def test_needs_more_fetches_validator_applies_to_headquarters() -> None:
    hq_unknown = _unknown_answer(status="needs_more_fetches")
    fr = FetchRequest(
        url="https://example.com/contact",
        reason="HQ city/country not on homepage; contact page would resolve.",
        expected_evidence=["headquarters"],
    )
    # passes when fetch_request matches:
    _good_profile(headquarters=hq_unknown, fetch_requests=[fr])

    # fails when no fetch_request matches:
    with pytest.raises(ValidationError, match="headquarters"):
        _good_profile(headquarters=hq_unknown, fetch_requests=[])


def test_needs_more_fetches_validator_applies_to_products_categories() -> None:
    pc_unknown = _unknown_list_answer(status="needs_more_fetches")
    with pytest.raises(ValidationError, match="products_categories"):
        _good_profile(products_categories=pc_unknown, fetch_requests=[])


# -- Readiness enum --------------------------------------------------------


def test_readiness_enum_accepts_known_values() -> None:
    for r in [
        "production",
        "low_rate_production",
        "prototype",
        "engineering_services",
    ]:
        _good_profile(readiness=_good_role_answer(value=r))


def test_readiness_enum_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        _good_profile(readiness=_good_role_answer(value="mass_production"))


# -- ndaa / blue_uas enums -------------------------------------------------


def test_ndaa_enum_yes_no_only() -> None:
    for v in ["yes", "no"]:
        _good_profile(ndaa=_good_role_answer(value=v))
    # 'unclear' was dropped: shades of unclear are expressed via status
    # (not_disclosed / unknown / needs_more_fetches) with value=null.
    with pytest.raises(ValidationError):
        _good_profile(ndaa=_good_role_answer(value="unclear"))
    with pytest.raises(ValidationError):
        _good_profile(ndaa=_good_role_answer(value="compliant"))


def test_blue_uas_enum_yes_no_only() -> None:
    for v in ["yes", "no"]:
        _good_profile(blue_uas=_good_role_answer(value=v))
    with pytest.raises(ValidationError):
        _good_profile(blue_uas=_good_role_answer(value="unclear"))
    with pytest.raises(ValidationError):
        _good_profile(blue_uas=_good_role_answer(value="cleared"))


def test_ndaa_not_disclosed_with_adjacent_evidence() -> None:
    """value=null + status=not_disclosed + cited adjacent claim is valid.

    This is the Adv Nav case: the site claims ITAR-free but doesn't claim
    NDAA. Encoding: value=null, status=not_disclosed, evidence=[ITAR cite].
    """
    answer = Answer(
        value=None,
        confidence="medium",
        status="not_disclosed",
        evidence=[_good_citation()],
        notes="site claims ITAR-free, not NDAA",
    )
    _good_profile(ndaa=answer)


def test_ndaa_unknown_with_no_evidence() -> None:
    """value=null + status=unknown + evidence=[] is valid.

    This is the no-signal case: no NDAA mention anywhere.
    """
    answer = Answer(
        value=None,
        confidence="low",
        status="unknown",
        evidence=[],
    )
    _good_profile(ndaa=answer)


# -- JSON Schema for tool registration -------------------------------------


def test_profile_submission_json_schema_contains_all_fields() -> None:
    schema = ProfileSubmission.model_json_schema()
    props = schema["properties"]
    for field in [
        "products_categories",
        "headquarters",
        "drone_supply_chain_role",
        "products",
        "ndaa",
        "blue_uas",
        "readiness",
        "unresolved_questions",
        "fetch_requests",
        "status",
    ]:
        assert field in props, f"missing field {field} in submission schema"


# -- ProfileSubmission -----------------------------------------------------


def _good_submission(**overrides: object) -> ProfileSubmission:
    base = dict(
        products_categories=_unknown_list_answer(),
        headquarters=_unknown_answer(),
        drone_supply_chain_role=_good_role_answer(),
        products=_unknown_list_answer(),
        ndaa=_unknown_answer(),
        blue_uas=_unknown_answer(),
        readiness=_unknown_answer(),
        unresolved_questions=[],
        fetch_requests=[],
        status="complete",
    )
    base.update(overrides)
    return ProfileSubmission(**base)  # type: ignore[arg-type]


def test_profile_submission_basic() -> None:
    sub = _good_submission()
    assert sub.drone_supply_chain_role.value == "oem"


def test_profile_submission_needs_more_fetches_validator() -> None:
    answer = _good_role_answer(
        value=None, status="needs_more_fetches", confidence="low", evidence=[]
    )
    with pytest.raises(ValidationError, match="fetch_request"):
        _good_submission(
            drone_supply_chain_role=answer,
            status="needs_more_fetches",
        )


# -- ProductDetail ---------------------------------------------------------


def _good_product(**overrides: object) -> ProductDetail:
    base = dict(
        name="Carrier H6 Hybrid",
        category="airframes",
        descriptor="gas-electric hybrid hexacopter UAV",
        granularity="sku",
        readiness="production",
        ndaa="yes",
        blue_uas="unknown",
        evidence=[_good_citation()],
        notes=None,
    )
    base.update(overrides)
    return ProductDetail(**base)  # type: ignore[arg-type]


def test_product_detail_basic() -> None:
    p = _good_product()
    assert p.name == "Carrier H6 Hybrid"
    assert p.category == "airframes"
    assert p.granularity == "sku"
    assert p.readiness == "production"


def test_product_detail_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        _good_product(category="autonomous_widgets")


def test_product_detail_rejects_unknown_granularity() -> None:
    with pytest.raises(ValidationError):
        _good_product(granularity="lineup")


def test_product_detail_readiness_enum_includes_unknown() -> None:
    """The products pass adds `unknown` to the vendor readiness enum since
    most product pages don't disclose maturity explicitly."""
    for r in [
        "production",
        "low_rate_production",
        "prototype",
        "engineering_services",
        "unknown",
    ]:
        _good_product(readiness=r)
    with pytest.raises(ValidationError):
        _good_product(readiness="vaporware")


def test_product_detail_ndaa_four_state() -> None:
    for v in ["yes", "no", "not_disclosed", "unknown"]:
        _good_product(ndaa=v)
    with pytest.raises(ValidationError):
        _good_product(ndaa="maybe")


def test_product_detail_blue_uas_four_state() -> None:
    for v in ["yes", "no", "not_disclosed", "unknown"]:
        _good_product(blue_uas=v)


def test_product_detail_descriptor_required() -> None:
    with pytest.raises(ValidationError):
        ProductDetail(  # type: ignore[call-arg]
            name="x",
            category="airframes",
            granularity="sku",
            readiness="production",
            ndaa="unknown",
            blue_uas="unknown",
            evidence=[],
        )


# -- ProductCatalogSubmission / ProductCatalog -----------------------------


def _good_catalog_submission(**overrides: object) -> ProductCatalogSubmission:
    base = dict(
        products=[_good_product()],
        unresolved_questions=[],
        fetch_requests=[],
        status="complete",
    )
    base.update(overrides)
    return ProductCatalogSubmission(**base)  # type: ignore[arg-type]


def test_catalog_submission_round_trip() -> None:
    sub = _good_catalog_submission()
    j = sub.model_dump_json()
    sub2 = ProductCatalogSubmission.model_validate_json(j)
    assert sub2 == sub


def test_catalog_submission_needs_more_fetches_requires_matching_request() -> None:
    fr = FetchRequest(
        url="https://x.example/products",
        reason="full SKU list missing",
        expected_evidence=["headquarters"],  # wrong field
    )
    with pytest.raises(ValidationError, match="products"):
        _good_catalog_submission(
            products=[],
            fetch_requests=[fr],
            status="needs_more_fetches",
        )


def test_catalog_submission_needs_more_fetches_passes_with_matching() -> None:
    fr = FetchRequest(
        url="https://x.example/products",
        reason="full SKU list missing",
        expected_evidence=["products"],
    )
    sub = _good_catalog_submission(
        products=[],
        fetch_requests=[fr],
        status="needs_more_fetches",
    )
    assert sub.status == "needs_more_fetches"


def test_catalog_from_submission_composes() -> None:
    sub = _good_catalog_submission()
    meta = ProfileMeta(
        model="claude-sonnet-4-6",
        num_turns=10,
        total_cost_usd=0.5,
        created_at="2026-05-02T00:00:00+00:00",
        extract_version="0.1.0",
    )
    cat = ProductCatalog.from_submission(
        sub,
        target_id="harris-aerial",
        run_id="r1",
        display_name="Harris Aerial",
        homepage_url="https://harrisaerial.com",
        corpus_root="/abs/path",
        meta=meta,
    )
    assert cat.target_id == "harris-aerial"
    assert len(cat.products) == 1
    assert cat.products[0].category == "airframes"


def test_catalog_submission_json_schema_contains_product_fields() -> None:
    """The schema is what's handed to the agent's submit tool."""
    schema = ProductCatalogSubmission.model_json_schema()
    assert "products" in schema["properties"]
    # Walk into the items definition for ProductDetail:
    defs = schema.get("$defs", {})
    pd = defs.get("ProductDetail", {})
    assert "name" in pd["properties"]
    assert "category" in pd["properties"]
    assert "readiness" in pd["properties"]
    assert "ndaa" in pd["properties"]
    assert "blue_uas" in pd["properties"]


def test_profile_from_submission_composes_full_profile() -> None:
    sub = _good_submission(unresolved_questions=["are they NDAA?"])
    meta = ProfileMeta(
        model="claude-sonnet-4-6",
        num_turns=5,
        total_cost_usd=0.10,
        created_at="2026-05-01T00:00:00+00:00",
        extract_version="0.1.0",
    )
    profile = Profile.from_submission(
        sub,
        target_id="harris-aerial",
        run_id="r1",
        display_name="Harris Aerial",
        homepage_url="https://harrisaerial.com",
        corpus_root="/abs/path",
        meta=meta,
    )
    assert profile.target_id == "harris-aerial"
    assert profile.unresolved_questions == ["are they NDAA?"]
    assert profile.drone_supply_chain_role.value == "oem"
    assert profile.ndaa.status == "unknown"
    assert profile.products_categories.items == []
