# UxVMarket Catalog Database — PRD

**Author:** Colin Steele
**Date:** April 2026
**Status:** v0.2, data-layer focus
**Supersedes:** v0.1 (generalization draft) and the original `competitive_intel_agent_prd.md` (avionics scope)

## Context

We built an internal competitive intelligence tool scoped to US merchant avionics electronics for UAS and eVTOL. The tool is operational through Phase 4a: 33 companies catalogued, a working weekly monitor loop, and a discovery queue producing real candidates. The left-half architecture (fetch, clean, extract, monitor, discover, all writing to a single SQLite store) is sound and portable.

v0.1 of this PRD generalized the scope from avionics to the full US UxV industrial base and introduced a public-facing product (UxVMarket) with an internal analytical dashboard, a public catalog site, and weekly public digests as the consumption surfaces.

**v0.2 narrows the current engagement to the data layer only.** No dashboard. No public site. No published digest. The UX surfaces remain in the long-term vision but are deferred indefinitely. Attention goes entirely to the hard problem: building the right database.

The database is the product. It is what any eventual consumption surface — UxVMarket, internal tools, analyst citations, matchmaking workflows — is built on top of. Everything that matters downstream depends on it being correct, normalized, complete, and defensible.

## Thesis

The hard work is not user-facing. It is structural. A buyer, an analyst, or a future UxVMarket surface can only be as trustworthy as the data layer underneath. That layer has three core entities:

**Players.** The firms that make things in the US UxV industrial base — their canonical identity, corporate structure, location, posture, leadership, and history.

**Products.** What each Player makes, at what readiness, with what spec depth, in what category, with what compliance posture.

**Pricesheets.** What products cost, where public, in what structure (SKU, list price, volume breaks, terms).

Initial focus is Players. Products and Pricesheets are downstream dependencies of a solid Players layer and are scoped but not built in the first phase of this engagement.

## Design principles

Two points of doctrine that govern the schema and the pipeline. Both are load-bearing decisions, not preferences.

**Facts / calibration split.** Evidence — the verbatim snippets and source URLs our extraction pipeline captures — does not belong in the facts tables. Evidence is calibration residue. It is useful for reviewing extraction quality, reconciling multi-source signals, and defending the catalog against a vendor's "that's wrong" complaint. It is not part of the shipped data model.

- **Facts layer** (what ships as the database): `players`, `products`, `pricesheets`, and the normalized child tables around them. Clean, canonicalized. No evidence columns. Each field holds a single reconciled value.
- **Calibration layer** (pipeline state, not shipped): `extraction_log`, `deltas`, `candidates`, `tool_calls`, raw fetched HTML snapshots. Captures everything the pipeline observed, including multi-source disagreements and historical state. Available for review, audit, and reconciliation. Stored in the same SQLite file for now, but explicitly not part of any eventual export or publication surface.

**Highly normalized schema.** The facts layer is normalized to at least 3NF throughout. Closed vocabularies (categories, domains, NDAA postures, readiness postures, ownership types, source types, certifications, agencies, NAICS codes) are reference tables, not free-text enums on the main tables. Repeating groups (aliases, locations, certifications held, leadership, funding rounds, SBIR awards, NAICS codes, flags) are separate child tables with foreign keys. Many-to-many relationships (investors to rounds, Players to domains, Players to certifications) use explicit join tables. Single source of truth for every fact; no redundant columns. The goal is that any query is a clean JOIN and any update touches one row.

The two principles reinforce each other. A normalized facts layer with no evidence columns is the cleanest thing we can hand to an eventual consumer. Calibration lives elsewhere.

## Goals

Build the Players facts table, populated from multi-source extraction and reconciliation, with canonical identity that survives rebranding, naming variance, and multi-category presence.

Establish the extraction-and-reconciliation pipeline that feeds the facts layer: fetch, clean, extract, reconcile. Extraction already exists; reconciliation is new.

Define the Products and Pricesheets schemas well enough that the Players work doesn't paint them into a corner, but do not build them yet.

Keep the monitoring and discovery loops operating on the Players catalog as it grows: weekly delta detection on the seed, weekly discovery passes on the agreed source list.

## Non-goals

**Any consumption surface.** No dashboards, no public site, no digest rendering, no review UI, no vendor self-service portal, no intelligence reports. All deferred. The database query layer is available for ad-hoc SQL; no pre-built renderers are in scope.

**Airframe manufacturers and system integrators as Players.** They appear in the database only as buyers / reference customers on supplier records, never as catalog entries themselves.

**Consumer drones and hobby-grade equipment.** Professional, defense-adjacent, or certification-track UxV only.

**Foreign vendors as primary Players.** Non-US suppliers appear only as flagged reference architectures or potential acquisition targets, in a separate visibility tier. Orqa and Evolito are the canonical examples from the current candidate queue.

**Vertical-specific deep analysis.** Horizontal catalog, not vertical reports.

## Taxonomy

Four axes organize Players and their Products. The taxonomy is versioned in the repo and earns revision through use.

**Domain applicability.** UAV, UGV, USV, UUV. Multi-domain suppliers tag all that apply.

**Subsystem category.** The twelve-category expansion from v0.1:

- Propulsion electronics (motor controllers, ESCs, integrated motor+ESC)
- Propulsion mechanical (bare motors, gearboxes, propellers, rotors)
- Power systems (battery cells, packs, BMS, PDUs, chargers)
- Flight and vehicle control (autopilots, flight computers, VMUs)
- Sensors and navigation (IMUs, GNSS, magnetometers, altimeters, air data)
- Payloads (EO/IR, SAR, LIDAR, communications payloads, munitions interfaces)
- Communications (datalinks, SATCOM modules, mesh radios, Remote ID beacons)
- Mechanical subsystems (landing gear, actuators, gimbals, release mechanisms, bay doors)
- Structures and materials (composite shops, additive manufacturing, structural test)
- Recovery systems (parachutes, flight termination, net capture)
- Ground segment (GCS hardware, GCS software, antennas, launch/recovery equipment)
- Test and measurement (HIL rigs, test equipment, qualification services)

**NDAA and trust posture.** NDAA-compliant, Blue UAS Framework listed, Blue UAS Cleared, not NDAA-eligible, unknown.

**Readiness posture.** Production, low-rate production, prototype-to-production, engineering services. New for v0.1, carries forward into v0.2.

A single Player can be active across multiple subsystem categories. The schema supports this through the Players-to-Products link rather than by attaching categories to Players directly.

## Data model sketch — Players (normalized)

Not final. Here to set the direction and surface early review. Every table below is 3NF; every closed vocabulary is a reference table; every repeating group is a child table with a foreign key.

### Reference tables (closed vocabularies)

```
subsystem_categories      (category_id PK, slug UNIQUE, display_name, description)
domains                   (domain_id PK, slug UNIQUE, display_name)
                          -- UAV, UGV, USV, UUV
ndaa_postures             (posture_id PK, slug UNIQUE, display_name)
                          -- ndaa_compliant, blue_uas_framework,
                          --   blue_uas_cleared, not_eligible, unknown
readiness_postures        (posture_id PK, slug UNIQUE, display_name)
                          -- production, low_rate, prototype, engineering_services
ownership_types           (ownership_id PK, slug UNIQUE, display_name)
                          -- independent, pe_backed, public, subsidiary,
                          --   nonprofit, government, unknown
certifications            (cert_id PK, slug UNIQUE, name, issuer)
                          -- AS9100, ISO_9001, ITAR_REGISTERED,
                          --   FACILITY_CLEARANCE_SECRET, ...
identifier_types          (identifier_type_id PK, slug UNIQUE, description)
                          -- cage, duns, ein, ticker, ...
agencies                  (agency_id PK, abbreviation UNIQUE, full_name)
                          -- DoD, USAF, DARPA, DHS, NASA, ...
naics_codes               (naics_code_id PK, code UNIQUE, description)
source_types              (source_type_id PK, slug UNIQUE, display_name, trust_tier)
                          -- sec_filing=1, sbir_gov=1, marketing_page=3, ...
round_types               (round_type_id PK, slug UNIQUE, display_name)
                          -- seed, series_a, ..., grant, acquisition
role_categories           (role_category_id PK, slug UNIQUE, display_name)
                          -- ceo, cto, cfo, coo, vp_eng, board, advisor, other
location_types            (location_type_id PK, slug UNIQUE, display_name)
                          -- hq, facility, office, manufacturing
```

### Core facts

```
players
  player_id                 PK
  canonical_name            the name used everywhere downstream
  legal_name                nullable
  ownership_type_id         FK -> ownership_types
  parent_player_id          FK -> players, nullable
  ndaa_posture_id           FK -> ndaa_postures
  readiness_posture_id      FK -> readiness_postures
  founded_year              nullable
  employee_count            nullable
  employee_count_observed_at nullable
  first_seen_at
  last_reconciled_at
```

### Child tables (one-to-many from players)

```
player_aliases
  alias_id                  PK
  player_id                 FK -> players
  alias
  is_legal_name             bool
  first_observed_at
  last_observed_at

player_locations
  location_id               PK
  player_id                 FK -> players
  location_type_id          FK -> location_types
  label                     nullable, e.g. "Huntsville engineering office"
  street, city, state_region, postal_code, country
  first_observed_at

player_identifiers
  player_id                 FK -> players
  identifier_type_id        FK -> identifier_types
  identifier_value
  PRIMARY KEY (player_id, identifier_type_id, identifier_value)

player_flags
  player_id                 FK -> players
  flag_slug
  PRIMARY KEY (player_id, flag_slug)
  -- closed set: non_us, prd_partner, low_confidence,
  --             airframer_customer_only, etc.
```

### Many-to-many bridges

```
player_naics
  player_id                 FK -> players
  naics_code_id             FK -> naics_codes
  is_primary                bool
  PRIMARY KEY (player_id, naics_code_id)

player_domains
  player_id                 FK -> players
  domain_id                 FK -> domains
  PRIMARY KEY (player_id, domain_id)

player_certifications
  player_cert_id            PK
  player_id                 FK -> players
  cert_id                   FK -> certifications
  certificate_identifier    nullable, where the Player quotes a specific cert number
  effective_date            nullable
  expiry_date               nullable
  first_observed_at
```

### People and leadership

```
people
  person_id                 PK
  canonical_name
  linkedin_url              nullable

player_person_roles
  role_id                   PK
  player_id                 FK -> players
  person_id                 FK -> people
  role_title                free text, the exact title observed
  role_category_id          FK -> role_categories
  start_date                nullable
  end_date                  nullable
  first_observed_at
```

### Funding

```
investors
  investor_id               PK
  canonical_name
  entity_type               vc | pe | corporate | government | individual | unknown

funding_rounds
  round_id                  PK
  player_id                 FK -> players
  round_type_id             FK -> round_types
  announced_date
  amount_usd                nullable (undisclosed rounds exist)
  post_money_valuation_usd  nullable

funding_round_investors
  round_id                  FK -> funding_rounds
  investor_id               FK -> investors
  is_lead                   bool
  PRIMARY KEY (round_id, investor_id)
```

### SBIR/STTR awards

```
sbir_awards
  award_id                  PK
  player_id                 FK -> players
  sbir_gov_award_number     UNIQUE, the government's own identifier
  topic_code
  phase                     I | II | III
  agency_id                 FK -> agencies
  fiscal_year
  amount_usd                nullable
  title
  start_date                nullable
  end_date                  nullable
  award_url                 nullable
```

Every field is nullable except the PKs, FKs to parent rows, and the canonical-name / identifier fields that are essential to distinguishing a row. "Not disclosed" is an acceptable outcome when sources are thin — it is represented by an absent child row, not a sentinel value on a parent row.

## Data model sketch — calibration layer

```
extraction_log
  log_id                    PK
  entity_type               'player' | 'product' | 'price' |
                            'player_alias' | 'player_location' |
                            'player_identifier' | 'player_certification' |
                            'player_person_role' | 'funding_round' |
                            'sbir_award' | ...
  entity_id                 FK into the relevant facts table, nullable during pre-reconciliation
  field_path                'canonical_name', 'street', 'amount_usd',
                            -- dotted paths not strictly needed once children are normalized,
                            -- but retained for cross-table consistency
  observed_value            the value this source reported (text)
  source_url
  source_snippet            verbatim text from the source
  source_type_id            FK -> source_types
  fetched_at
  superseded_by_log_id      FK -> extraction_log, nullable
```

Multiple rows per `(entity_type, entity_id, field_path)` when multiple sources report. Reconciliation is a separate step that reads the log, applies rules (source-trust ordering via `source_types.trust_tier`, recency, field-specific heuristics, manual override), and writes the chosen value to the facts table. When normalization means the fact is itself a row (e.g., a funding round), the log records the row's field-by-field provenance; the reconciliation step may also decide to create or suppress the fact row based on cross-source agreement.

`deltas`, `candidates`, and `tool_calls` remain from the current implementation and continue to serve as pipeline/operations state, not facts.

## Pipeline

Unchanged in shape from the current implementation, extended at two points.

**Fetch.** httpx + Playwright fallback. Content hashing on cleaned text (change from current, which hashes raw HTML). Stale snapshots archived. Bot-blocked and JS-heavy sites route to the manual_entry path, not simply logged as failures.

**Clean.** BeautifulSoup strip to visible text plus link anchors. Unchanged.

**Extract.** Messages API with Sonnet 4.6 and a category-generalized extraction prompt. Output schema aligned with the facts tables but written to `extraction_log`, not directly to the facts tables.

**Reconcile.** New step. Reads `extraction_log` for a given entity, applies reconciliation rules, writes the chosen canonical value to the facts table. Rules: source-trust ordering (SEC > SBIR.gov > marketing page > trade press), recency preference, manual override precedence. Conflicts beyond the rules' resolution are flagged for human review.

**Monitor.** Weekly hash-gated re-fetch with the classifier detecting substantive changes. Writes to `deltas`. Substantive deltas can trigger reconciliation passes on affected entities.

**Discover.** Raw Messages API + server-side web_search per the Phase 4a pattern. Writes to `candidates`. Approved candidates promote into `players`.

**Manual entry.** A human-in-the-loop path, authored separately from the pipeline. Records written with `source_type = 'manual_entry'` and full provenance (author, date, citations). Treated by reconciliation like any other source, with its own trust level.

## Phase plan (data-layer only)

**Phase 5a — Schema migration and cleanup (weeks 1-2).**
- Migrate current schema: rename `companies` → `players`, remove `evidence_json` columns from facts tables.
- Introduce `extraction_log` table. Backfill from current `evidence_json` blobs so provenance isn't lost.
- Generalize `prompts/system.md` extraction prompt to the 12-category taxonomy and the Players attribute shape.
- Delete vestigial Agent SDK code (`hooks.py`, unused prompts) and document the raw-API discovery as canonical.
- Fetch failures: split into URL-corrections (fixable in the crawler) and manual_entry candidates.

**Phase 5b — Canonical identity and reconciliation (weeks 3-6).**
- Add `player_id` canonical identity. Backfill existing rows. Define aliases and establish manual-override precedence.
- Build the reconciliation step that reads `extraction_log` and writes canonical values to `players`.
- Introduce the `manual_entry` authoring path (initially as a SQL insert workflow; any UI deferred).
- First seeded Players populated through the new pipeline end-to-end: avionics (already seeded) reconciled against multi-source evidence.

**Phase 5c — Category expansion (months 2-4).**
- One category seeded per month through the pipeline: propose order payloads → communications → power systems expansion → sensors and navigation → propulsion mechanical.
- Each seeding pass: discovery run with category-specific prompt → AUVSI roster cross-reference → SBIR award search → human review of merged candidates → seed confirmation → extraction and reconciliation.
- Non-US reference tier accumulates per category with explicit flag.

**Phase 6 — Products layer (months 5-8).**
- Extend the pipeline to populate `products` per Player. Category-specific attribute schemas finalized.
- Products join to Players via `player_id`.
- Reconciliation rules extended to product attributes.

**Phase 7 — Pricesheets (month 9+).**
- Pricesheet schema finalized. Extraction pattern for published SKU + price pages.
- Many Players do not publish prices; the table will be sparse and that is acceptable.

## What doesn't change

The operating pattern. Propose, approve, refine. Terse communication. Honest flags on uncertainty. Human approval gates on any stable-seed edit.

The two-part boundary. Left half writes the DB. Right half reads. There is no right half in this engagement, but the boundary stays enforced so that whoever builds one later is working against the same contract.

The weekly cadence for monitoring and the on-demand cadence for discovery.

The evidence discipline — with the v0.2 clarification that evidence lives in the calibration layer, not the facts layer. The pipeline's obligation to capture verbatim snippets and source URLs for every extracted value is unchanged. What's different is where those end up.

## Open questions parked

Where the manual_entry authoring UI lives when it becomes necessary. Probably a thin CLI or SQL-template initially; any interactive UI is a deferred UX decision.

How reconciliation rules get versioned. Rule changes can alter the facts layer retroactively; that implies a rule-run history is itself something the calibration layer should track.

Whether the taxonomy is versioned alongside the code or as a separate document with its own release cadence. Probably alongside for now, since only internal tools consume it.

---

*v0.2. Narrows v0.1 to the data layer. UX / public surfaces deferred indefinitely. Next revision after Phase 5a completes and the first reconciliation pass runs end-to-end.*
