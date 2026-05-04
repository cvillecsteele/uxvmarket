"""Pydantic schema for extracted supplier profiles.

The profile answers seven questions about a UXV supplier (the eighth, "can
we cite specific mirrored pages as evidence?", is satisfied by the
`Citation` requirement on every answer).

The schema is shaped to grow without rewrites:

- `Citation.source_kind` is a `Literal` that will gain `sbir`, `crunchbase`,
  `google_places`, etc. as those evidence sources come online. v1 only
  emits `mirror`.
- `Answer[T]` for single-valued questions; `ListAnswer[T]` for enumerated
  ones (categories, products). Both share the same `confidence`/`status`
  vocabulary so the generic `needs_more_fetches` validator can apply
  uniformly.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import normalize_country, normalize_us_state


SourceKind = Literal["mirror"]
"""Future: 'sbir', 'crunchbase', 'google_places'. v1 only emits 'mirror'."""

Confidence = Literal["low", "medium", "high"]

AnswerStatus = Literal[
    "answered",
    "not_disclosed",
    "needs_more_fetches",
    "unknown",
]

ProfileStatus = Literal[
    "complete",
    "partial",
    "needs_more_fetches",
    "failed",
]

Role = Literal[
    "oem",
    "subsystem_supplier",
    "component_supplier",
    "software_platform",
    "service_provider",
    "reseller",
    "integrator",
    "broad_industrial",
    "none",
]

PrimaryCategory = Literal[
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
]

Readiness = Literal[
    "production",
    "low_rate_production",
    "prototype",
    "engineering_services",
]

NDAAStatus = Literal["yes", "no"]
BlueUASStatus = Literal["yes", "no"]


# -- Product-pass types ----------------------------------------------------
# The products pass uses flat fields per ProductDetail rather than Answer[T]
# wrappers because each product carries one umbrella `evidence` list backing
# all its claims. The four-state encoding still applies: `not_disclosed`
# means the site addresses adjacent compliance (ITAR-free, Made in USA)
# without claiming NDAA for this product; `unknown` means no signal.

ProductReadiness = Literal[
    "production",
    "low_rate_production",
    "prototype",
    "engineering_services",
    "unknown",
]

NDAAProductStatus = Literal["yes", "no", "not_disclosed", "unknown"]
BlueUASProductStatus = Literal["yes", "no", "not_disclosed", "unknown"]
ProductGranularity = Literal["sku", "family", "category"]


# Field names that appear in `expected_evidence` of FetchRequest. The
# `needs_more_fetches` validator uses this list to enforce that any answer
# with that status has a matching fetch_request.
ANSWER_FIELDS: tuple[str, ...] = (
    "products_categories",
    "headquarters",
    "drone_supply_chain_role",
    "products",
    "ndaa",
    "blue_uas",
    "readiness",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Citation(StrictModel):
    """A citation pointing to a contiguous line range in a mirrored text file.

    The agent emits `(resource_id, line_start, line_end)`. The `snippet`
    field is *hydrated* by the runner from the actual file contents — the
    agent never types snippet text, so verbatim is guaranteed by
    construction. `url` and `page_class` are likewise informational fields
    that the runner can fill from the corpus's `crawl_index`.
    """

    source_kind: SourceKind = "mirror"
    resource_id: str = Field(
        description="For mirror: e.g. 'resource-0001'. Future source kinds "
        "will use their own id space (SBIR award number, Crunchbase uuid, "
        "etc.) and may use a different locator instead of line_start/end."
    )
    line_start: int = Field(
        ge=1,
        description="1-indexed first line of the cited range, inclusive.",
    )
    line_end: int = Field(
        ge=1,
        description="1-indexed last line of the cited range, inclusive. "
        "Must be >= line_start.",
    )
    url: str | None = None
    page_class: str | None = None
    snippet: str = Field(
        default="",
        description="Verbatim text from line_start..line_end of the cited "
        "resource. Hydrated by the runner; do not provide.",
    )

    @model_validator(mode="after")
    def _line_range_sane(self) -> Citation:
        if self.line_end < self.line_start:
            raise ValueError(
                f"line_end ({self.line_end}) must be >= "
                f"line_start ({self.line_start})"
            )
        return self


T = TypeVar("T")


class Answer(StrictModel, Generic[T]):
    value: T | None
    confidence: Confidence
    status: AnswerStatus
    evidence: list[Citation] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _answered_requires_evidence(self) -> Answer[T]:
        if self.status == "answered" and not self.evidence:
            raise ValueError("evidence must be non-empty when status='answered'")
        return self


class ListAnswer(StrictModel, Generic[T]):
    """For questions whose answer is a list (categories, products).

    `items` is the agent's enumeration; each item carries its own evidence
    and per-item confidence. The wrapper's `confidence` reflects belief in
    list completeness/representativeness.
    """

    items: list[T] = Field(default_factory=list)
    confidence: Confidence
    status: AnswerStatus
    notes: str | None = None

    @model_validator(mode="after")
    def _answered_requires_items(self) -> ListAnswer[T]:
        if self.status == "answered" and not self.items:
            raise ValueError(
                "items must be non-empty when status='answered'; "
                "use 'not_disclosed' or 'unknown' for empty enumerations"
            )
        return self


class Headquarters(StrictModel):
    city: str | None = None
    state_or_province: str | None = None
    country: str
    address: str | None = None

    @field_validator("country")
    @classmethod
    def _norm_country(cls, value: str) -> str:
        return normalize_country(value)

    @field_validator("state_or_province")
    @classmethod
    def _norm_state(cls, value: str | None) -> str | None:
        return normalize_us_state(value)


class CategoryClaim(StrictModel):
    category: PrimaryCategory
    is_primary: bool
    confidence: Confidence
    evidence: list[Citation] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _evidence_required(self) -> CategoryClaim:
        if not self.evidence:
            raise ValueError(
                f"CategoryClaim(category={self.category!r}) requires at "
                "least one Citation in evidence — listing a category IS "
                "an assertion and must be cited"
            )
        return self


class ProductMention(StrictModel):
    """Lightweight per-vendor-pass product reference.

    Used in `Profile.products` (vendor pass) for a quick enumeration. The
    richer per-product extraction lives in `ProductDetail` (products pass).
    """

    name: str
    product_type: str | None = None
    confidence: Confidence
    evidence: list[Citation] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _evidence_required(self) -> ProductMention:
        if not self.evidence:
            raise ValueError(
                f"ProductMention(name={self.name!r}) requires at least one "
                "Citation in evidence — naming a product IS an assertion "
                "and must be cited"
            )
        return self


class ProductDetail(StrictModel):
    """Fully-classified product entry from the products pass.

    One umbrella `evidence` list backs name + descriptor + category claims.
    The maturity / ndaa / blue_uas literals are agent-emitted; in the
    common drone-industry case where the site says nothing, expect
    `unknown` for the latter two.
    """

    name: str
    category: PrimaryCategory
    descriptor: str = Field(
        description="One-line prose summary, e.g. "
        "'800 V heavy-lift motor controller'."
    )
    granularity: ProductGranularity = Field(
        description="`sku` for a single product (Carrier H6 Hybrid), "
        "`family` for a product line (chainflex® cables), "
        "`category` for an even-coarser bucket (UAV recovery systems)."
    )
    readiness: ProductReadiness
    ndaa: NDAAProductStatus
    blue_uas: BlueUASProductStatus
    evidence: list[Citation] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _evidence_required(self) -> ProductDetail:
        if not self.evidence:
            raise ValueError(
                f"ProductDetail(name={self.name!r}) requires at least one "
                "Citation in evidence — every product entry IS an "
                "assertion and must be cited"
            )
        return self


class FetchRequest(StrictModel):
    url: str
    reason: str = Field(
        description="Why this URL would help. Must reference what evidence "
        "is missing, not just 'more info'."
    )
    expected_evidence: list[str] = Field(
        description="Profile field names this URL would help resolve. "
        f"Allowed values: {', '.join(ANSWER_FIELDS)}."
    )
    source_hint: str | None = Field(
        default=None,
        description="How the agent identified this URL: 'skipped in crawl_index', "
        "'linked from resource-0001', 'guessed legal page', etc.",
    )
    in_corpus_index: bool = Field(
        default=False,
        description="True if this URL is already present in crawl_index "
        "(skipped/failed) and just needs to be re-prioritised.",
    )

    @field_validator("expected_evidence")
    @classmethod
    def _expected_evidence_recognised(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError(
                "expected_evidence must list at least one field name; "
                f"valid values: {', '.join(ANSWER_FIELDS)}"
            )
        unknown = [v for v in value if v not in ANSWER_FIELDS]
        if unknown:
            raise ValueError(
                f"expected_evidence contains unknown field name(s) "
                f"{unknown!r}; valid values: {', '.join(ANSWER_FIELDS)}"
            )
        return value


class ProfileMeta(StrictModel):
    model: str
    num_turns: int
    total_cost_usd: float | None = None
    created_at: str
    extract_version: str


# ---------------------------------------------------------------------------
# Cross-field validators — shared between ProfileSubmission and Profile.


def _check_needs_more_fetches(
    *,
    answers: dict[str, Answer[Any] | ListAnswer[Any]],
    fetch_requests: list[FetchRequest],
) -> None:
    """For each answer field whose status is `needs_more_fetches`, require
    at least one FetchRequest that lists that field name in
    expected_evidence."""
    for field_name, answer in answers.items():
        if answer.status != "needs_more_fetches":
            continue
        if not any(
            field_name in fr.expected_evidence for fr in fetch_requests
        ):
            raise ValueError(
                f"{field_name}.status='needs_more_fetches' but no "
                f"fetch_request lists '{field_name}' in expected_evidence"
            )


def _check_products_categories_primary(claims: ListAnswer[CategoryClaim]) -> None:
    """`products_categories` answered → exactly one item.is_primary=True."""
    if claims.status != "answered":
        return
    primaries = [c for c in claims.items if c.is_primary]
    if len(primaries) != 1:
        raise ValueError(
            f"products_categories status='answered' requires exactly one "
            f"item with is_primary=True; got {len(primaries)}"
        )


# ---------------------------------------------------------------------------


class ProfileSubmission(StrictModel):
    """Agent-owned slice of the profile, captured via the submit_profile tool.

    The runner combines this with deterministic corpus metadata and run-time
    `ProfileMeta` to produce the final `Profile`.
    """

    products_categories: ListAnswer[CategoryClaim]
    headquarters: Answer[Headquarters]
    drone_supply_chain_role: Answer[Role]
    products: ListAnswer[ProductMention]
    ndaa: Answer[NDAAStatus]
    blue_uas: Answer[BlueUASStatus]
    readiness: Answer[Readiness]

    unresolved_questions: list[str] = Field(default_factory=list)
    fetch_requests: list[FetchRequest] = Field(default_factory=list)
    status: ProfileStatus

    @model_validator(mode="after")
    def _validate(self) -> ProfileSubmission:
        _check_needs_more_fetches(
            answers=self._answers(),
            fetch_requests=self.fetch_requests,
        )
        _check_products_categories_primary(self.products_categories)
        return self

    def _answers(self) -> dict[str, Answer[Any] | ListAnswer[Any]]:
        return {
            "products_categories": self.products_categories,
            "headquarters": self.headquarters,
            "drone_supply_chain_role": self.drone_supply_chain_role,
            "products": self.products,
            "ndaa": self.ndaa,
            "blue_uas": self.blue_uas,
            "readiness": self.readiness,
        }


class Profile(StrictModel):
    target_id: str
    run_id: str
    display_name: str
    homepage_url: str
    corpus_root: str

    products_categories: ListAnswer[CategoryClaim]
    headquarters: Answer[Headquarters]
    drone_supply_chain_role: Answer[Role]
    products: ListAnswer[ProductMention]
    ndaa: Answer[NDAAStatus]
    blue_uas: Answer[BlueUASStatus]
    readiness: Answer[Readiness]

    unresolved_questions: list[str] = Field(default_factory=list)
    fetch_requests: list[FetchRequest] = Field(default_factory=list)
    status: ProfileStatus
    # Editorial 1-3 sentence summary written post-hoc by a cheap pass
    # (Haiku) given the structured profile + products + homepage copy.
    # Populated by the tagline pass at the end of the extract pipeline;
    # None on profiles that haven't been run through it.
    tagline: str | None = None
    meta: ProfileMeta

    @model_validator(mode="after")
    def _validate(self) -> Profile:
        _check_needs_more_fetches(
            answers=self._answers(),
            fetch_requests=self.fetch_requests,
        )
        _check_products_categories_primary(self.products_categories)
        return self

    def _answers(self) -> dict[str, Answer[Any] | ListAnswer[Any]]:
        return {
            "products_categories": self.products_categories,
            "headquarters": self.headquarters,
            "drone_supply_chain_role": self.drone_supply_chain_role,
            "products": self.products,
            "ndaa": self.ndaa,
            "blue_uas": self.blue_uas,
            "readiness": self.readiness,
        }

    @classmethod
    def from_submission(
        cls,
        submission: ProfileSubmission,
        *,
        target_id: str,
        run_id: str,
        display_name: str,
        homepage_url: str,
        corpus_root: str,
        meta: ProfileMeta,
    ) -> Profile:
        return cls(
            target_id=target_id,
            run_id=run_id,
            display_name=display_name,
            homepage_url=homepage_url,
            corpus_root=corpus_root,
            products_categories=submission.products_categories,
            headquarters=submission.headquarters,
            drone_supply_chain_role=submission.drone_supply_chain_role,
            products=submission.products,
            ndaa=submission.ndaa,
            blue_uas=submission.blue_uas,
            readiness=submission.readiness,
            unresolved_questions=submission.unresolved_questions,
            fetch_requests=submission.fetch_requests,
            status=submission.status,
            meta=meta,
        )


# -- Triage pass (priority list) -------------------------------------------


class ProductPriority(StrictModel):
    """One product the triage agent thinks worth full extraction.

    Triage does NOT cite evidence — its job is to identify candidates
    and rank them. The downstream products pass verifies via Citation.
    """

    name: str = Field(
        description="Product name as it appears on the vendor site."
    )
    relevance_score: int = Field(
        ge=1,
        le=10,
        description="1–10. 10 = central to characterizing the UxV "
        "industrial supplier base in US/allied countries; "
        "1 = peripheral / catalog filler / off-topic.",
    )
    rationale: str = Field(
        description="One sentence explaining the score."
    )


class ProductPrioritySubmission(StrictModel):
    """Agent's triage output, ordered most-relevant first."""

    products: list[ProductPriority] = Field(default_factory=list)
    notes: str | None = None


class ProductPriorityList(StrictModel):
    """Final triage artifact, with runner-applied truncation recorded."""

    target_id: str
    run_id: str
    display_name: str
    homepage_url: str
    corpus_root: str
    products: list[ProductPriority] = Field(default_factory=list)
    agent_listed: int = Field(
        description="Number of products the agent originally identified, "
        "before runner truncation."
    )
    max_products: int = Field(
        description="The cap applied by the runner."
    )
    # Default `complete` for backward compat with legacy files written
    # before the field existed (those triage runs either completed or
    # failed; no partial concept then). New runs always set this
    # explicitly via from_submission.
    status: ProfileStatus = "complete"
    notes: str | None = None
    meta: ProfileMeta

    @classmethod
    def from_submission(
        cls,
        submission: ProductPrioritySubmission,
        *,
        target_id: str,
        run_id: str,
        display_name: str,
        homepage_url: str,
        corpus_root: str,
        max_products: int,
        meta: ProfileMeta,
        status: ProfileStatus = "complete",
    ) -> ProductPriorityList:
        agent_listed = len(submission.products)
        truncated = submission.products[:max_products] if max_products > 0 else submission.products
        return cls(
            target_id=target_id,
            run_id=run_id,
            display_name=display_name,
            homepage_url=homepage_url,
            corpus_root=corpus_root,
            products=truncated,
            agent_listed=agent_listed,
            max_products=max_products,
            status=status,
            notes=submission.notes,
            meta=meta,
        )


# -- Products pass containers -----------------------------------------------


class ProductCatalogSubmission(StrictModel):
    """Agent-owned slice of the products-pass output."""

    products: list[ProductDetail] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    fetch_requests: list[FetchRequest] = Field(default_factory=list)
    status: ProfileStatus

    @model_validator(mode="after")
    def _needs_more_fetches_consistency(self) -> ProductCatalogSubmission:
        if self.status != "needs_more_fetches":
            return self
        if not any(
            "products" in fr.expected_evidence for fr in self.fetch_requests
        ):
            raise ValueError(
                "status='needs_more_fetches' but no fetch_request lists "
                "'products' in expected_evidence"
            )
        return self


class ProductCatalog(StrictModel):
    """Final products-pass output, persisted alongside `profile.json`."""

    target_id: str
    run_id: str
    display_name: str
    homepage_url: str
    corpus_root: str
    products: list[ProductDetail] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    fetch_requests: list[FetchRequest] = Field(default_factory=list)
    status: ProfileStatus
    meta: ProfileMeta

    @classmethod
    def from_submission(
        cls,
        submission: ProductCatalogSubmission,
        *,
        target_id: str,
        run_id: str,
        display_name: str,
        homepage_url: str,
        corpus_root: str,
        meta: ProfileMeta,
    ) -> ProductCatalog:
        return cls(
            target_id=target_id,
            run_id=run_id,
            display_name=display_name,
            homepage_url=homepage_url,
            corpus_root=corpus_root,
            products=submission.products,
            unresolved_questions=submission.unresolved_questions,
            fetch_requests=submission.fetch_requests,
            status=submission.status,
            meta=meta,
        )
