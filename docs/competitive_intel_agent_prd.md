# Competitive Intelligence Agent — PRD (Sketch v0.1)

**Author:** Colin Steele
**Status:** Draft for review
**Last updated:** 2026-04-23

## Problem

Allocortech lacks a structured, ongoing view of its competitive landscape. The closest analog shops (US, NDAA-eligible, merchant avionics electronics for UAS and eVTOL) are actively shipping, pivoting, and entering the market, and the company's current mental model of "who else is out there" is whatever happens to surface through LinkedIn, trade press, and customer conversations. That model decays every quarter. Product positioning, BD prioritization, channel partner conversations, and eventually the capital raise all rest on an understanding of the competitive set that is not being maintained on a cadence.

The question this tool answers is not "who are our competitors" at a point in time. It is "what changed this week, and what's new that we didn't know about."

## Goals

Maintain a curated catalog of the US merchant avionics-electronics competitive set, segmented by product category, power tier, and NDAA posture. Surface substantive changes (new SKUs, certification claims, partnerships, funding events, senior hires, site-level pivots) weekly. Surface candidate new entrants weekly, with evidence and a proposed segment assignment, gated on human approval before catalog ingestion. Provide a queryable dashboard for ad-hoc sourcing questions like "who's claiming DO-178C conformance on a motor controller right now" or "which competitors have added 800V product lines in the past six months."

## Non-goals

Customer intelligence, which is a separate problem with different sources and different consumers. Patent landscape monitoring, which requires different data and different legal review. Sales-lead generation. Automated outbound of any kind. Replacing human judgment on positioning or strategy. This tool produces structured inputs to decisions. It does not make decisions.

## Users

The primary digest consumer is the COO, weekly. The ad-hoc query consumer is BD leadership (Mike Blades) and, situationally, engineering leadership when sourcing a component or evaluating a partner. The review-and-approve consumer, for both discovery candidates and catalog deltas, is the COO at least in v1. Future consumers include board and investor-facing summary views during capital raise cycles, which are a distinct surface and out of scope for v1.

## Scope of the catalog

The catalog is segmented along three axes. The first is product category, with initial values of flight control, flight termination, motor control, power distribution, battery management, and integrated propulsion. The second is power tier, which applies to motor control and is bucketed as sub-5kW, 5 to 50kW, 50 to 500kW, and 500kW-plus, with voltage class (nominal 28V, 400V, 800V) as a secondary attribute. The third is NDAA posture, with values of NDAA-compliant, Blue UAS Framework listed, Blue UAS Cleared List, not NDAA-eligible, and unknown.

Company-level attributes include HQ location, estimated headcount, ownership status (independent, PE-backed, public, subsidiary), SBIR and STTR award history, disclosed funding rounds, and known customer or partner names where public. Product-level attributes include category, power and voltage class where applicable, weight, form factor, supported protocols (DroneCAN, ARINC825, DShot, PWM, others), claimed TRL, claimed certifications or standards conformance (DO-178C, DO-254, AS9100, MIL-STD-461, MIL-STD-810), and disclosed price where available. Every row in every table carries evidence fields (source URL, verbatim snippet, fetch timestamp) for auditability.

## Architecture

The system splits cleanly into two halves joined by a single storage layer.

The **left half** is a batch data-production pipeline. It fetches public web content, extracts structured attributes, classifies deltas, synthesizes discovery candidates, and writes to SQLite. It runs on a schedule, has no UI, and never reads from the consumption side.

The **right half** is read-only presentation over the same database: a weekly markdown digest, a web dashboard, and a CLI. It never fetches from the public web and never calls the Messages API directly. All of its work is queries against the stored catalog, delta history, and candidate queue.

The **interface** between the two halves is the SQLite schema. Four tables carry the load: `companies` and `products` (current catalog state), `deltas` (history of substantive changes with evidence), and `candidates` (discovery rows awaiting approval). The only back-channel from right to left is human review actions taken through the dashboard — approving a candidate flips it into `companies`, accepting a proposed delta commits it to catalog state. Every other write is left-to-right.

### Production pipeline (left half)

Three loops run at different cadences against the storage layer.

The **catalog loop** is the stable core. A hand-curated seed list of 40 to 60 named companies, maintained in a source-controlled file, drives the initial population. An LLM extraction pass using the Messages API pulls company-level and product-level attributes from each company's marketing pages into the schema. The seed list is the source of truth. Automation populates attributes. Humans approve changes to the seed itself.

The **monitoring loop** runs weekly. For each cataloged company, it fetches a small set of canonical URLs (home, products index, each product detail page, news or press, team or about). Content is hashed. When a hash changes, a classifier pass asks whether the delta is substantive in the specific sense of new SKU, new cert claim, new partnership, funding event, material team change, or site-level strategic pivot, versus noise like copyright year ticks or hero image swaps. Substantive deltas go into the weekly digest with the specific before-and-after text and a link. Noise is suppressed.

The **discovery loop** runs on a weekly or on-demand cadence and is the place where the agent pattern earns its keep. An orchestrator prompt articulates the competitive set definition (US, NDAA-eligible, merchant avionics electronics, the specific power and voltage tiers, explicit exclusions for airframers, motor-only vendors, and payload-only vendors) and passes the current seed list as exclusion context. Worker subagents run parallel web_search and web_fetch calls against narrow high-signal sources (DIU Blue UAS Framework updates, SBIR.gov awards in relevant NAICS codes, AUVSI exhibitor rosters, trade press, job-posting aggregators filtered for specific skill combinations, recent press releases). LinkedIn is explicitly out of scope as a source in v1 to avoid ToS and procurement entanglements. Results are synthesized into structured candidate rows with name, URL, proposed segment, one-sentence thesis, and two or three evidence snippets. Candidates enter a review queue. The COO approves or rejects. Approved candidates join the seed.

## Safety constraints

Mapping to Anthropic's published framework for agent development ([reference](https://www.anthropic.com/news/our-framework-for-developing-safe-and-trustworthy-agents)).

**Human control.** Discovery produces candidates, not catalog commits. Monitoring produces proposed deltas, not applied ones. Any change to the stable seed list requires human review and approval through the dashboard. The agent has read access to the public internet and write access only to its own local database. It does not touch Google Workspace, Gmail, Drive, Slack, or any other company system in v1. If internal-context enrichment becomes desirable later, it is a separate tool with a separate permission surface and a separate review, not a feature flag on this one.

**Transparency.** Every surfaced row carries evidence. Source URL, verbatim snippet, fetch timestamp, and the specific classifier or agent decision that generated the surfacing. The dashboard exposes these by default. Tool calls are logged for retroactive audit.

**Alignment.** Scope is constrained through prompt discipline. Sharp category definition, explicit exclusions, and an explicit seed list of known peers are passed on every run. A per-run token cap and a weekly dollar cap fail closed. If a run exceeds budget, it returns what it has and stops rather than looping.

**Privacy.** Not materially relevant in v1 because all inputs are public web pages. Becomes relevant if scope expands. Guarded by the architectural boundary that this tool doesn't touch internal systems.

**Security.** The fetched-content-as-untrusted-input discipline is the central concern. All fetched text is treated as data, not instructions. It is never inlined inside the system prompt boundary. Suspicious instruction-shaped content in fetched pages is flagged and stripped before being passed to the next agent hop. The tool's runtime identity has no credentials to external systems beyond the Anthropic API key and public web access. Every tool call is logged. The budget caps above are also a security control, since an agent caught in an injection-induced loop is an agent spending money.

## Consumption surfaces (right half)

Three surfaces, in order of implementation priority. All three are thin clients over the same query API against the SQLite schema described above.

The **weekly digest** is a markdown file committed to a GitLab repo on a Monday-morning schedule. It diffs against the prior week and highlights substantive changes, new candidates awaiting review, and a top-line summary of catalog size and coverage. This is the always-on artifact and the one Colin actually reads.

The **dashboard** is a lightweight web UI served from the same repo, providing filtering by segment and attribute, a company-detail view with change history, a review queue for discovery candidates, and a set of saved queries. Mockup below. This is the surface used for ad-hoc questions and for the weekly review session.

The **CLI** is a scriptable interface over the same queries, for one-off automation and for building custom reports into strategic memos. Low priority for v1. Worth scaffolding so the dashboard and digest are both thin layers over the same query API.

## Tech stack

Python 3.12 across both halves. Storage is SQLite, with the database file committed to git on a daily snapshot for history and diff-ability.

Model and harness choice splits by loop. The **catalog** and **monitoring** loops call the Anthropic Messages API directly, using Sonnet 4.6. These are structured extraction over known URLs: no agent loop, no tool-use beyond our own fetchers, no reason to pay for an agent harness. Fetching uses Playwright for JS-rendered pages and httpx for static ones.

The **discovery** loop uses the Claude Agent SDK (Python), where the orchestrator-worker pattern, parallel tool use, and hook-based controls are native. Opus 4.7 is the orchestrator for reasoning depth; Sonnet 4.6 subagents are defined via `AgentDefinition` and invoked in parallel through the `Agent` tool. The SDK's built-in `WebSearch` and `WebFetch` tools replace what would otherwise be a hand-rolled harness. Hooks are where the safety constraints are implemented concretely: `PreToolUse` hooks enforce per-run token caps and strip instruction-shaped content from fetched pages before it reaches the next agent hop; `PostToolUse` hooks log every tool call with arguments and result for retroactive audit. Permissions are scoped through the SDK's `allowed_tools` whitelist so the discovery agent can search, fetch, and write candidate rows to SQLite, and nothing else.

Scheduling is GitLab CI for the weekly loops and a local cron for ad-hoc runs. The dashboard is a minimal server-rendered HTML app (FastAPI plus vanilla templates, no framework churn). All prompts, schemas, and seed lists live in the repo as flat files.

## API references

- Messages API reference: https://docs.claude.com/en/api/messages
- Tool use overview: https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview
- Web search tool: https://docs.claude.com/en/docs/agents-and-tools/tool-use/web-search-tool
- Web fetch tool: https://docs.claude.com/en/docs/agents-and-tools/tool-use/web-fetch-tool
- Agent SDK (formerly Claude Code SDK): https://docs.claude.com/en/api/agent-sdk
- Prompt caching (material for cost control on long system prompts): https://docs.claude.com/en/docs/build-with-claude/prompt-caching
- Multi-agent research pattern (Anthropic engineering): https://www.anthropic.com/engineering/multi-agent-research-system
- Safe agents framework: https://www.anthropic.com/news/our-framework-for-developing-safe-and-trustworthy-agents

## Source references

- DIU Blue UAS Framework list (discovery source): https://www.diu.mil/latest/blue-uas-refresh-list-and-framework-platforms-and-capabilities-selected
- SBIR/STTR award search: https://www.sbir.gov/awards
- AUVSI exhibitor directory: https://www.auvsi.org/
- FAA Part 108 NPRM context (regulatory driver for segment expansion): https://www.dlapiper.com/en-us/insights/publications/2025/10/faa-proposed-part-108-bvlos-rule

## Implementation phases

The plan builds the left half first, end to end, before any right-side surface beyond the weekly digest. Phase 1 is a narrow spike whose purpose is a go/no-go decision on extraction quality, not construction. Week labels on phases 2-5 are relative to phase 1 passing; if the spike fails, those phases slide behind a prompt-and-schema rework before the clock restarts.

**Phase 1 — Extraction spike (a few days).** Five hand-picked companies across two product categories. Minimal schema: `companies` and `products` only, each with evidence fields (source URL, verbatim snippet, fetch timestamp). Messages API with Sonnet 4.6 runs the extraction prompt against static marketing pages via httpx. Output lands in a local SQLite file. Every extracted row is hand-reviewed against its source URL for factual correctness, snippet faithfulness, and hallucination. The exit criterion is a yes/no: is the extraction output trustworthy enough to scale, or does the prompt or schema need fundamental rework before going further. No Playwright, no CI, no monitoring, no discovery, no dashboard, no digest.

**Phase 2 — Catalog loop at full scale (week 1).** Full 40-60 company seed list finalized. Schema frozen for all four tables (`companies`, `products`, `deltas`, `candidates`). Playwright added for pages httpx cannot render. The catalog loop runs end-to-end and populates the database. This is the first version of "we know who's out there."

**Phase 3 — Monitoring loop and first digest (weeks 2-3).** Weekly hash-gated re-fetches of canonical URLs per company. Substantive-change classifier tuned against a hand-labeled delta set. Safety controls appropriate to the left-half shape — per-run token caps on the extraction and classifier prompts, and a tool-call log table in SQLite — in place before monitoring runs in anger. First weekly markdown digest generated and committed to GitLab. At this point the left half is producing value through a single always-on artifact with no UI.

**Phase 4 — Discovery loop and dashboard (weeks 4-5).** Agent SDK-based discovery loop operational against a narrow set of sources, writing rows to the `candidates` table. Discovery-specific safety envelope lands here, since this is where the Agent SDK enters the system: `PreToolUse` hooks enforce per-run budget caps and strip instruction-shaped content from fetched pages before it reaches the next agent hop, `PostToolUse` hooks write per-tool-call audit rows, and `allowed_tools` scoping restricts the discovery agent to search, fetch, and writes to the `candidates` table. Dashboard v1 ships in parallel with filtering, company-detail view, change history, and the review queue. First cycle of candidate approval by COO. The right half of the architecture now exists.

**Phase 5 — Coverage expansion and handoff (weeks 6-8).** Additional discovery sources plugged in. Saved-query library built out. CLI scaffolded. Handoff documentation written so operational ownership can transfer to a BD hire or program manager without the tool becoming a black box.

## Open questions

Who owns operational review once the tool is live. The tool's weekly value decays quickly if no human is named to triage the queue and read the digest. Default assumption is COO through M3, then transfer to BD. This needs confirmation.

Whether the catalog is single-tenant (ACT only) or eventually usable by channel partners. If we ever want to share filtered views with ePropelled or Plettenburg, the data model needs to support tenant-scoped visibility from the start. Adding it later is expensive.

Budget ceiling for the monthly API spend. Rough estimate is $100 to $400 per month at v1 cadence and v1 catalog size. Needs a cap, which needs someone to sign it.

---

*v0.1. For internal review. Revise against Chad and Brian's input before scoping the build.*
