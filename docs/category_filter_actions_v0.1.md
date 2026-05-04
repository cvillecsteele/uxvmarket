# Action list — Catalog category and filter changes

**Date:** April 2026
**Source:** Decisions from PRD review session, April 25.
**Scope:** Schema, taxonomy, filter UX, and data hygiene changes implied by the seven decisions.

## Decisions (recap)

1. **Payloads splits into 3 top-level categories**: ISR Payloads, Electronic Warfare, Munitions. Mounting moves to Mechanical Subsystems.
2. **Avionics is added as a meta-category filter**: selecting it returns the union of Flight Control + Sensors and Navigation + Communications.
3. **Flight Termination Systems (FTS)** is promoted to its own top-level category.
4. **NDAA filter splits into two**: a Compliance hierarchy and a Country-posture filter.
5. **"Flagged" is removed from the public-facing UI** entirely (internal data-quality state, not user-relevant).
6. **Country posture is ITAR-friendly via a negative list**: explicit exclusion of China, Russia, Iran, North Korea, Cuba, Syria, Belarus, Venezuela, Myanmar.
7. **Public surfacing is restricted to US + NATO + MNNA + AUKUS**: vendors outside this allowlist remain in the database but are suppressed from public views.

Per the in-conversation rename: ISR Payloads (not "Sensors") and "Munitions" stays as the third payload bucket name (covering release mechanisms and stores alongside ordnance proper).

---

## Category model changes

### A. Top-level taxonomy revision

The category list goes from 12 to 14:

- Propulsion Electronics
- Propulsion Mechanical
- Power Systems
- Flight and Vehicle Control
- Sensors and Navigation
- ISR Payloads *(new — sensing portion of old Payloads)*
- Electronic Warfare *(new — EW/SIGINT portion of old Payloads)*
- Munitions *(new — mission/munition/release portion of old Payloads)*
- Communications
- Mechanical Subsystems *(now also receives Mounting from old Payloads)*
- Structures and Materials
- Recovery Systems
- Flight Termination *(promoted from sub-bucket of Recovery)*
- Ground Segment
- Test and Measurement

(That's 15 — I miscounted as 14 earlier. Either we accept 15 as the final count, or we collapse Flight Termination back under Recovery Systems with strong sub-tagging. Confirm before implementing.)

### B. Avionics meta-category

A virtual filter that aggregates three primary categories. Implemented as a query-time UNION rather than a stored category. Schema implication: the category model needs a distinction between *primary* categories (where players are tagged) and *meta* categories (which derive their membership from one or more primary categories).

### C. Multi-category tagging on players

Confirmed required: a player like ARK Electronics legitimately spans Flight Control, Power Systems, and Sensors and Navigation. The schema's `player_flags` table can carry category tags but the public filter UX needs a deliberate decision about whether ARK is counted once in the totals or once per category. Default proposal: counted in each category it claims, with a unique-player count shown separately on the totals badge.

---

## Filter UX changes

### D. Compliance filter (hierarchy, single-select)

Values, in nesting order:

- All
- NDAA-compliant
- Blue UAS Framework (subset of NDAA-compliant)
- Blue UAS Cleared (subset of Framework)

Selecting a deeper level filters more strictly. Selecting "NDAA-compliant" returns Framework and Cleared too. The filter is single-select because the values are nested.

### E. Country posture filter (single-select)

Values:

- All (US + NATO/MNNA/AUKUS)
- US-headquartered only
- NATO/MNNA/AUKUS only

Vendors HQ'd outside this allowlist do not appear in any of the public-facing options, even "All." They exist in the database but are filtered out at render time.

### F. Readiness filter (single-select)

Values:

- All
- Production
- Low-rate production
- Prototype
- Engineering services
- Readiness: unknown *(per your earlier call)*

The "unknown" value is its own filter option, which is honest about the catalog's current data state and lets buyers explicitly include or exclude unknowns.

### G. Removed: "Flagged"

No longer appears in public UI. Internal data-quality flags continue to exist in the database for operator use.

### H. Sector → Category (label change)

The dimension currently labeled "Sector" in the UI gets renamed to "Category" or "Subsystem." Pick one before implementation.

---

## Schema additions

### I. Category model

Two new tables (or columns) to support primary-vs-meta:

- `categories` table with `slug`, `display_name`, `is_meta`, `description`. Existing categories migrated in as `is_meta=false`. Avionics added as `is_meta=true`.
- `meta_category_members` table linking meta categories to the primary categories they aggregate. Avionics → (flight_vehicle_control, sensors_navigation, communications).

### J. Country and excluded-list model

- `countries` table with `iso2`, `name`, `is_excluded` (the negative list), `is_surfaced` (US + NATO/MNNA/AUKUS).
- `players.country_iso2` populated from existing HQ data.
- A view `surfaced_players` that filters by both flags and is what all public-facing renderers query against.

### K. New category records

Seven schema-level inserts for the new categories:

- `isr_payloads`
- `electronic_warfare`
- `munitions`
- `flight_termination`
- `avionics` (meta)
- The Mounting subcategory tag added to `mechanical_subsystems`
- Sunset of the old `payloads` category (records re-tagged, slug deprecated)

---

## Data migration

### L. Re-tag existing payload-tier players

Walk the current 10 payload-tier players and reassign:

- Sentera, Ouster, GeoCue, NextVision, LiDAR USA, GreenSight, Gremsy → `isr_payloads`
- Aerora, RPX, Orbital Research → unchanged for now (they're suspect entries; defer to DLQ review)

No EW or munitions players in the catalog yet. Those categories launch empty pending the next discovery run.

### M. Country-tag the existing 152 players

Populate `country_iso2` for every player. This is mostly mechanical from the existing HQ city/state data, but ~43 players have null HQ. For those, run a light-touch enrichment pass that pulls the country from the website footer or about page; failing that, mark `country_iso2 = NULL` and exclude from surfacing until resolved.

Apply `is_excluded` and `is_surfaced` derivation:

- US, NATO members, MNNA, AUKUS → surfaced
- China, Russia, Iran, North Korea, Cuba, Syria, Belarus, Venezuela, Myanmar → excluded
- Everywhere else (e.g. Vietnam, India, Singapore, Brazil) → not surfaced, not excluded — kept in the database but not visible in the public UI

### N. Identify and handle existing non-surfaced records

From the current catalog: Gremsy (Vietnam) is the one I can identify off-hand. There may be others under non-Five-Eyes / non-MNNA flags that need re-classification under the new rule. A one-time audit pass surfaces them.

### O. Promote existing FTS-adjacent players

Re-tag from Recovery Systems to Flight Termination:

- PacSci EMC
- Kratos Defense (UAS FTS Division)
- AVSS (Aerial Vehicle Safety Solutions)
- ParaZero — *currently flagged non_us; check if it's surfaced*

Likely 4-6 players in the new category at launch.

---

## Discovery prompt updates

### P. Replace the `discovery_payloads.md` prompt

Split into three new prompts:

- `discovery_isr_payloads.md` — focused on EO/IR, LiDAR, SAR, multispectral, hyperspectral
- `discovery_electronic_warfare.md` — focused on EW, SIGINT, jamming, counter-UAS sensors, RF-collection payloads
- `discovery_munitions.md` — focused on loitering munitions, warheads, release mechanisms, stores management, drop systems

The existing `discovery_payloads.md` is archived (do not delete; useful as a regression test).

### Q. New `discovery_flight_termination.md` prompt

Focused on independent FTS, geofence enforcement, range safety termination, post-recovery beacons.

### R. Update country posture in all 14 prompts

Replace the existing "US-preferred (NATO-aligned acceptable with `non_us` flag). Exclude Chinese, Russian..." paragraph with the new ITAR-friendly definition: "US-headquartered preferred. NATO/MNNA/AUKUS-aligned acceptable. Exclude vendors HQ'd in [negative list countries]. Vendors HQ'd outside both lists may be proposed but flag them with `country_unsurfaced` so they enter the database without surfacing publicly."

---

## UI implementation (Astro static site)

### S. Filter component restructuring

The current filter row is a single 5-value flat filter. Becomes:

- Compliance (single-select hierarchy, 4 values)
- Country posture (single-select, 3 values)
- Readiness (single-select, 6 values)
- Category (single-select from the 14-item list, plus the Avionics meta)

The filter labels change from "Sector" / "NDAA" to "Category" / "Compliance" / "Country" / "Readiness."

### T. Category navigation

Decide whether the 14 primary categories appear as a flat list (long, scrollable) or grouped (Avionics meta on top, then sub-categories indented; or grouped by phase of system — propulsion-related, control-related, payload-related, support). The flat version is simpler and faster to ship; grouped is more browsable as the catalog grows. Default: ship flat for v1, revisit at 200+ players.

### U. Player-detail page badges

Each player page renders the structural attributes that drive filters: compliance posture, country, readiness, categories claimed. The "Flagged" indicator does not appear here for the public site. Internal-only render adds it.

---

## Sequence and dependencies

The action list breaks into four work blocks. Each block is independently shippable; later blocks depend on earlier ones.

**Block 1 — Schema and data foundation (1-2 days)**

- Add `categories`, `meta_category_members`, `countries` tables (Items I, J)
- Migrate existing categories into the new `categories` table
- Add `country_iso2` to players, populate from existing HQ data (Item M, partial)
- Add the seven new category records (Item K)
- Define the negative list and the surfacing list as data, not as code constants

**Block 2 — Data migration and re-tagging (1 day)**

- Re-tag existing payload-tier players to ISR Payloads (Item L)
- Promote existing FTS-adjacent players to Flight Termination (Item O)
- Audit and country-tag the remaining ~43 null-HQ players (Item M, completion)
- Identify any existing players that fall outside the surfacing rule and verify their disposition (Item N)
- Sunset the old `payloads` category slug (Item K)

**Block 3 — Discovery prompt updates (1 day)**

- Split `discovery_payloads.md` into three new prompts (Item P)
- Add `discovery_flight_termination.md` (Item Q)
- Update country-posture language in all 14 prompts (Item R)
- Run each new prompt once to validate output and seed the new categories

**Block 4 — UI changes (2-3 days)**

- Restructure the filter component (Item S)
- Implement category navigation (Item T)
- Implement readiness filter UI with explicit "unknown" option (Item F)
- Update player-detail badges (Item U)
- Rename "Sector" → "Category" (Item H)
- Remove "Flagged" from public UI (Item G)

Total: 5-7 working days.

---

## Open items

The 14-vs-15 category count question (does Flight Termination promote to its own top-level, or stay under Recovery Systems with strong sub-tagging?) — I recommended 15 above because that's what the decision implies, but worth a final confirmation since it changes the visual density of the filter chip row.

The "Sector" rename target — Category vs. Subsystem. Both are accurate; Category is slightly more familiar to general users, Subsystem is more accurate for a sourcing audience. Lean Category for v1.

The multi-category counting policy — whether ARK Electronics counts as 1 or as 3 (one per category claimed) in the totals badges. Lean toward "shown in each category it claims, unique count badged separately" for transparency.

The disposition of "low_confidence" flagged players in the public UI — the call was to remove "Flagged" as a filter, but individual flagged records still exist. Default: surface them normally (the flag is operator-internal; it doesn't change buyer-visible state) unless a flag is severe enough to warrant exclusion (e.g., `excluded`, `hallucinated`).

---

*v0.1. Action list pending confirmation on the 14-vs-15 count and the "Sector" rename target.*
