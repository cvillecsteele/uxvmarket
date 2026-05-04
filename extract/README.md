# extract

`extract` reads mirrored vendor corpora produced by the sibling
`mirroring/` package and emits cited supplier profiles, structured
product catalogs, and editorial taglines. Output is canonicalized
into `vendors/<slug>/profile.json` + `products.json` for the
frontend generator to consume.

## What this package does

For each vendor, the pipeline runs four passes:

| pass | model | what it does | output file |
| --- | --- | --- | --- |
| **profile** | Sonnet | seven structured questions, cited from corpus | `profile.json` |
| **triage** | Sonnet | stack-rank products by relevance to UxV supplier base | `products-priority.json` |
| **products** | Sonnet | full ProductDetail per top-N (default 15) products | `products.json` |
| **tagline** | Haiku | 1–3 sentence editorial summary, given profile + products + homepage | written into `profile.json`'s `tagline` field |

Profile + triage use atomic `submit_*` MCP tools (latest-wins on
re-submission). Products uses incremental `add_product` +
`finalize_product_catalog` tools so partial output survives caps.
Tagline uses the Anthropic SDK directly (one-shot, no MCP).

## Subcommands

```
uxv-extract profile              # one vendor: profile pass
uxv-extract products             # one vendor: triage + products + (auto) tagline
uxv-extract tagline              # one vendor: tagline only (refresh / fill-in)
uxv-extract batch                # whole run: profile + triage + products + tagline per vendor
uxv-extract loop                 # round-B: aggregate fetch_requests → mirror → re-extract
uxv-extract followups            # aggregate fetch_requests across a run into JSONL
uxv-extract canonicalize         # merge per-run outputs into vendors/<slug>/
uxv-extract migrate-citations    # one-time: rewrite per-run citations to canonical IDs
```

`profile`, `products`, and `tagline` accept either
`--vendor-slug <slug>` (reads canonical evidence from
`vendors/<slug>/website/`) or the legacy
`--run-id + --target-id + --workspace-root` triplet (reads per-run
mirror corpus). The two flag groups are mutually exclusive.

`batch` and `loop` operate on a mirror run-id directly.

## Schema

| field                     | type                                          |
| ------------------------- | ---------------------------------------------- |
| `products_categories`     | ListAnswer of `CategoryClaim` (16 categories, one `is_primary`) |
| `headquarters`            | Answer of `Headquarters` (city, state, country, address) |
| `drone_supply_chain_role` | Answer of `Role` (8 values: `oem`, `subsystem_supplier`, …) |
| `products`                | ListAnswer of `ProductMention` |
| `ndaa`                    | Answer of `yes`/`no` (or `null` with `not_disclosed`/`unknown`) |
| `blue_uas`                | Same shape as `ndaa` |
| `readiness`               | Answer of `production`/`low_rate_production`/`prototype`/`engineering_services` |
| `tagline`                 | `str | None` — populated by the tagline pass |
| `status`                  | `complete`/`partial`/`needs_more_fetches`/`failed` |

Every Answer carries `evidence: [Citation]` where each Citation is
`(source_kind, resource_id, line_start, line_end)`. The runner extracts
the snippet from `text/NNNN-*.txt` so verbatim is guaranteed by
construction. Snippet cap: 60 words per citation.

`Citation.source_kind = Literal["mirror"]` today, designed to grow
to `sbir`, `crunchbase`, `google_places`, etc. without rewriting
consumers.

## Caps and partial-on-cap

Cost and time are constrained at two levels.

### Per-vendor (every `profile`/`products`/`batch` invocation)

| flag                   | default | purpose                                    |
| ---------------------- | ------: | ------------------------------------------ |
| `--max-turns`          | 30 (100 for products pass) | caps agent loop round trips |
| `--max-cost-usd`       | **2.50** | SDK-enforced hard stop on USD spend (do not raise without approval) |
| `--timeout-sec`        | 600     | wall-clock ceiling for the **whole vendor**, shared across all passes |

Important: `--timeout-sec` is the **TOTAL** budget across profile +
triage + products. Each pass receives `remaining = budget -
elapsed_so_far` as its own timeout. When the budget exhausts
mid-pass, the **partial-on-cap** path kicks in and the runner writes
whatever was produced up to that point with `status="partial"`.

`add_product` is incremental (one product per call, server-side
accumulator) so a cap mid-products gives you whatever products were
already submitted. `submit_profile` and `submit_product_priority`
are atomic but latest-wins, so a cap after the first refinement
gives you that submission.

### Aggregate (`batch` only)

| flag                          | default      | purpose                          |
| ----------------------------- | ------------ | -------------------------------- |
| `--max-batch-cost-usd`        | 300.0        | stop when cumulative spend hits this |
| `--max-batch-runtime-sec`     | 43200 (12h)  | stop after this much wall clock  |
| `--max-consecutive-failures`  | 5            | stop after the last N completed targets all failed (rolling window) |
| `--concurrency`               | 4            | max extractions in flight at once |

Use `2.50 × N + ~10% slack` for `--max-batch-cost-usd`, not round
numbers. Example: 50 vendors → `--max-batch-cost-usd 130`.

### Fan-out semantics

With `--concurrency=N`, up to N vendors run in parallel via
`asyncio.Semaphore`. When an aggregate cap fires, **in-flight
extractions finish** but no new extractions start. Worst-case
overshoot is `(concurrency - 1) × --max-cost-usd`.

Partials count as completions — they do NOT trip
`max_consecutive_failures`. Only hard errors and aborted runners
count as failures.

## Common workflows

### Single vendor end-to-end (already mirrored + canonical website exists)

```bash
uxv-extract profile  --vendor-slug honeywell
uxv-extract products --vendor-slug honeywell --max-products 15
uxv-extract tagline  --vendor-slug honeywell
```

Each writes to `extract/output/runs/<slug>-canonical/<slug>/`.
Then `uxv-extract canonicalize honeywell` merges into
`vendors/honeywell/`.

### Bulk: 50 vendors from a mirror run

```bash
uxv-extract batch \
  --run-id be-be2-possible-players-50-20260501 \
  --workspace-root /path/to/mirroring \
  --target-id <each slug> \
  --include-products --max-products 15 \
  --max-cost-usd 2.50 --max-batch-cost-usd 130 \
  --timeout-sec 600 --concurrency 4
```

`--include-products` enables the triage + products + tagline
passes. With `include_tagline=True` (default), the tagline pass
auto-runs after products.

### Round-B refinement (loop)

After a stage-A batch, the agent flagged URLs it wanted in
`fetch_requests`. The `loop` subcommand aggregates them, mirrors
them, and re-extracts:

```bash
source ~/.zshrc                               # ANTHROPIC_API_KEY + BROWSERLESS_API_KEY
uxv-extract loop \
  --source-run-id be-be2-possible-players-50-20260501 \
  --new-run-id   be-be2-cohort50-fu1 \
  --workspace-root /path/to/mirroring \
  --mirror-cli /path/to/mirroring/.venv/bin/uxv-mirror \
  --target-id <each slug to refine> \
  --include-products --max-products 15 \
  --max-cost-usd 2.50 --max-batch-cost-usd 90 \
  --timeout-sec 600 --concurrency 4 \
  --max-mirror-calls-per-target 15
```

Vendors with no fetch_requests are naturally skipped. Tip: use
`uxv-extract followups --run-id <source>` first to dump the
followups JSONL and pick a high-value subset rather than
re-running every vendor.

### Canonicalize per-vendor outputs into vendors/

After stage A (and optionally stage B):

```bash
uxv-extract canonicalize <slug1> <slug2> ...
# or:
uxv-extract canonicalize        # all slugs with extract output anywhere
```

For each slug:
- discover all `extract/output/runs/*/<slug>/{profile,products}.json`
- merge oldest first, upsert field-by-field per the rules below
- migrate citations to canonical resource IDs from
  `vendors/<slug>/website/url_id_map.json`
- write to `vendors/<slug>/profile.json` + `products.json`
- write `vendors/<slug>/canonicalize_report.json` with merge
  provenance and citation-drift counts

Field-merge rules:
- Answer/ListAnswer fields: `answered > unknown`; later answer
  beats earlier; never downgrade
- `fetch_requests` and `unresolved_questions`: newer always wins
- top-level `status`: take the BEST across runs
  (`complete > partial > needs_more_fetches > failed`)
- `tagline`: keep the first non-null encountered (oldest first)
- products list: union by `name`; newer entry replaces older on
  collision

Schema strictness (StrictModel) prevents adding merge-metadata
fields like `_canonicalize_note` or `merged_from_runs` to the
output JSON. All such provenance lives in
`canonicalize_report.json`.

## Defaults

- Profile/triage model: `claude-sonnet-4-6` (override with `--model`)
- Tagline model: `claude-haiku-4-5`
- Per-vendor cost ceiling: `$2.50` (do NOT raise without approval)
- Per-vendor wall-clock: 600s total across all passes

`ANTHROPIC_API_KEY` must be in env. The `loop` subcommand
shells out to `uxv-mirror`, which needs `BROWSERLESS_API_KEY`.
Source `~/.zshrc` before running batch loops if your keys live
there.

## Operator gotchas (learned the hard way)

- **Tagline timeouts** (Anthropic API hiccups) happen. Tagline
  failures are isolated; batch keeps going. Backfill with
  `uxv-extract tagline --target-id <slug>` after.
- **0-fetched-page mirrors** produce near-zero-cost
  `needs_more_fetches` profiles. That's the agent's honest signal,
  not a bug. Expect ~$0.05 per such vendor.
- **Single-line citations >60 words** (paragraph-on-one-line
  pages) get rejected. The error message tells the agent to pick
  a different line, not retry. Without that guidance the agent
  burns turns thrashing.
- **$0-cost partials** for some products passes are anomalies in
  SDK cost reporting, not data quality issues. Real products data
  is on disk.
- The 7 vendors that consistently produce $0 partials in stage B:
  re-running them won't change the outcome; their canonical
  evidence is what it is.

## Dev

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest    # 280+ tests
```
