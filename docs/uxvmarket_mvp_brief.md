# UxVMarket — MVP Brief

**Author:** Colin Steele
**Date:** April 2026
**Status:** v0.1, pre-build

## What benefit must we create

The US domestic UxV industrial base is forming under statutory pressure (NDAA, EO 14307, FCC Covered List, the ASDA) and a demand discontinuity measured in billions. The supply side is fragmented, under-capitalized, and operationally invisible. The demand side is bewildered. A program manager with six weeks on the clock and a gap in a block diagram cannot find the four US shops that could supply them. They ask a friend. If the friend doesn't know, they grant a waiver and the whole forcing function leaks.

The benefit UxVMarket creates is **legibility of the domestic UxV supplier base to the people who need to source from it.** Four layers, each earning the next.

**Visibility.** The segment exists as a published, opinionated taxonomy with named participants. Today it doesn't. MarketsandMarkets describes a different segment. DIU's Blue UAS list is compliance infrastructure, not sourcing infrastructure. The AUVSI directory is a rolodex. A buyer searching today does not find a canonical answer. UxVMarket publishes the canonical answer.

**Discoverability.** A buyer with a spec (category, power tier, voltage, NDAA posture, certification posture, quantity, timeline) can filter the population down to the three to seven shops that plausibly qualify. Today this takes weeks of phone calls. UxVMarket takes it to minutes.

**Trust.** The data is accurate, the provenance is visible, the editorial voice is literate, the operation is reliable. None of this is purchasable. T = f(time). The only defense is cadence and accuracy from the first week forward.

**Matchmaking.** A buyer posts a structured spec and the platform routes it to qualifying vendors with the metadata to learn from every interaction. This is the north star. The earlier layers are scaffolding toward it. Every architectural decision made before matchmaking exists should be evaluated against whether it supports or constrains matchmaking later.

The composite benefit: domestic UxV sourcing becomes a legible market rather than a game of who-knows-whom. That's the thing that doesn't exist, and it's the thing the industry is about to need badly.

## The vocabulary posture

UxVMarket does not inherit MarketsandMarkets' vocabulary or DIU's or AUVSI's. It imposes its own, derived from the tiering in the Tannhäuser analysis: Tier 1 propulsion (motors, controllers, ESCs), Tier 2 power (cells, packs, management), Tier 3 control (flight controllers, sensors). Airframes and system integrators are out of scope. This is opinionated and defensible. The taxonomy is the category-defining artifact and it ships with v1.

## What is the MVP

A single-page site and a weekly email. That's it.

**The site** publishes the player list. Seventy to ninety US-based NDAA-eligible UxV component suppliers across the three tiers. Each entry: name, HQ, one-paragraph description, tier and category tags, NDAA posture, public certification status, URL, last-reviewed date. No product detail yet. No pricing yet. No vendor accounts yet. The taxonomy document is linked in the header. A corrections email address is prominent.

**The email** goes out every Monday morning. It contains the week's deltas: new entrants surfaced by the internal tool, substantive changes to existing entries, newly published certification or Blue UAS inclusions, SBIR awards relevant to the segment, and one short editorial paragraph. Five to ten minutes to read. Gated by email signup; the list is the asset.

**What the MVP is not:** it is not a product database, not a comparison tool, not a vendor self-service portal, not a matchmaking platform. Those come later in named stages. The MVP establishes the two things that matter at launch — the taxonomy and the cadence — and nothing else.

The test of whether the MVP is working: at month six, do three hundred people in the right roles open the Monday email.

## Stages to the MVP, and past it

**Stage 0 — Internal tool (now through month three).**
The competitive intelligence tool Colin is already building runs against the seed list, produces the weekly digest internally, and matures its extraction and classification accuracy against a private audience of one. No public site. No public email. Six to eight weeks of private dogfooding against the real dataset proves the cadence is survivable and the data is clean enough to publish.

**Stage 1 — MVP launch (month four).**
Site live at uxvmarket.com with the player list. First public Monday email ships. Methodology document published. Corrections process in place. Entity and neutrality posture resolved before launch, not after. Guest list of initial subscribers seeded from Colin's existing industry network.

**Stage 2 — Product listings, best-effort (months five through eight).**
The internal tool's extraction pipeline populates product-level data under each vendor. Each product carries a "compiled from public sources on [date]" provenance note. No pricing claims beyond what vendors publish themselves. Filterable by category, tier, power class, voltage class, NDAA posture, certification posture. The discoverability layer goes live.

**Stage 3 — Claim your company (months eight through twelve).**
Vendors verify domain-based affiliation and get admin rights to their own pages. They can edit, upload datasheets, correct specs, add SKUs. Vendor-authored changes are visibly distinguished from tool-compiled content. Claim is free. The base listing is always free. This is the pivot point where the site transitions from a one-directional publication to a participatory infrastructure.

**Stage 4 — Editorial depth (months twelve through eighteen).**
The weekly email matures into a layered publication: weekly deltas free and ungated, monthly intelligence reports email-gated, biannual market analysis pieces, annual State of the Domestic UxV Supplier Base report. The editorial voice earns the right to snark. Guest contributions begin, curated carefully.

**Stage 5 — Matchmaking (month eighteen onward).**
Structured RFQ flow. Buyers submit specs through the platform, the platform routes to qualifying vendors, metadata on responses flows back. This is the north star and it is only credible after the earlier layers have established the trust that makes buyers willing to route through the platform and vendors willing to respond. Nothing about matchmaking is architected after Stage 4 begins. The data model, the vendor page structure, and the buyer journey are all designed to accommodate matchmaking from Stage 1, so Stage 5 is an activation, not a rebuild.

## What the MVP depends on

The internal competitive intelligence tool must exist and be reliable by Stage 0 completion. The MVP is a publication surface on top of the tool, not a separate build. If the tool runs, the MVP is cheap. If the tool doesn't run, the MVP is a second job.

The neutrality posture must be resolved before Stage 1. Three options, in order of increasing separation from ACT: disclosed ACT sponsorship, Iron Brothers Ventures framing with ACT as advisor, or structurally independent entity with industry advisory board. The choice shapes everything downstream and retrofitting it is expensive.

The operator's time commitment for the first eighteen months must be real. Four to eight hours per week assuming the internal tool feeds the publication. Twelve to twenty if it doesn't. The winter is survivable at the first number and not at the second.

## Open questions parked for later

Revenue. Governance. Capitalization. Staffing past the operator. Whether Stage 5 matchmaking is a feature of UxVMarket or a separate entity that licenses the data. Whether ACT's listing appears on the site and how. What happens if a vendor threatens to delist over unfavorable editorial.

None of these block the MVP. All of them become decisions between Stage 3 and Stage 5. The discipline is to not pre-answer them before the data of running the earlier stages is available.

---

*v0.1. Next revision after the internal tool is running and the first three private weekly digests have shipped.*
