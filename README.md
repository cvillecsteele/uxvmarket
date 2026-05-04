# UxV

UxV Market source workspace. The canonical vendor corpus lives in `vendors/`;
the Astro site in `fe/site/` is generated from that corpus.

## Layout

```
mirroring/         # Browserless-based vendor evidence acquisition
extract/           # LLM extraction (profile + triage + products + tagline)
vendors/<slug>/    # canonical per-vendor source of truth
  website/         # mirrored evidence (raw HTML, JSON, markdown, text, documents)
  profile.json     # structured 7-question profile + tagline
  products.json    # structured ProductDetail catalog
  canonicalize_report.json
fe/generator/      # Python: vendors/ → Astro content collections
fe/site/           # Astro: content → static HTML
```

The frontend generator no longer reads the legacy `be/` or `be2/` SQLite
catalog outputs (those directories were removed; archived under
`/tmp/be-*` and `/tmp/be2-*` if needed).

## Operator Workflow

End-to-end: getting one or more vendors from "URL on a list" to "rendered card on the FE."

### 1. Mirror the vendor (acquire evidence)

```bash
cd mirroring
.venv/bin/uxv-mirror mirror \
  --target "Honeywell=https://aerospace.honeywell.com" \
  --profile serious_vendor \
  --max-calls-per-target 50
```

Or batch-mirror many at once with `--target-file targets.jsonl`. See
`mirroring/README.md` for profile sizes (`quick_evidence`, `serious_vendor`,
`full_audit`) and the JSONL schema. Browserless does the rendering;
mirroring writes per-run corpora under
`mirroring/output/runs/<run-id>/targets/<slug>/`.

`mirror` and `resume` **auto-promote** at the end of every run, populating
`vendors/<slug>/website/` with stable canonical resource IDs. Manual
re-promote: `uxv-mirror promote <slug>`.

### 2. Extract structured data (one or many)

```bash
cd extract
.venv/bin/uxv-extract batch \
  --run-id be-be2-possible-players-50-20260501 \
  --workspace-root /Users/colinsteele/Projects/uxv/mirroring \
  --target-id honeywell --target-id <next> ... \
  --include-products --max-products 15 \
  --max-cost-usd 2.50 --max-batch-cost-usd 130 \
  --timeout-sec 600 --concurrency 4
```

Per vendor this runs four passes (profile → triage → products → tagline)
with per-vendor wallclock budget shared across all passes. Caps that
fire mid-pass produce **partial-on-cap** output (never empty). Cost
ceiling is **$2.50/vendor**; aggregate `--max-batch-cost-usd` should be
`2.50 × N + ~10% slack`.

### 3. Stage B refinement (optional, for high-gap vendors)

After stage A, the agent flags pages it wanted in `fetch_requests`. Pick
a subset (e.g. those with >10 fetch_requests) and run:

```bash
source ~/.zshrc                              # BROWSERLESS_API_KEY in env
.venv/bin/uxv-extract loop \
  --source-run-id be-be2-possible-players-50-20260501 \
  --new-run-id be-be2-cohort50-fu1 \
  --workspace-root /Users/colinsteele/Projects/uxv/mirroring \
  --mirror-cli /Users/colinsteele/Projects/uxv/mirroring/.venv/bin/uxv-mirror \
  --target-id <high-gap slug> ... \
  --include-products --max-products 15 \
  --max-cost-usd 2.50 --max-batch-cost-usd 90 \
  --timeout-sec 600 --concurrency 4
```

This aggregates fetch_requests, mirrors the missing pages, and
re-extracts. Vendors with no fetch_requests skip naturally.

### 4. Canonicalize into `vendors/<slug>/`

```bash
.venv/bin/uxv-extract canonicalize <slug> <slug> ...
# or to canonicalize every slug with extract output:
.venv/bin/uxv-extract canonicalize
```

Merges per-run profile.json + products.json across stage A and stage B
runs (oldest first; field-by-field upsert per the rules in
`extract/README.md`), migrates citations to canonical resource IDs,
writes to `vendors/<slug>/profile.json` + `products.json` + sidecar
`canonicalize_report.json`.

### 5. Generate the site

```bash
cd fe/generator && uv run python -m generator
cd fe/site && npm run build
```

(`npm run build` runs the generator first via the `sync-content` script.)

### Cost ballparks

- Per vendor (full pipeline, stage A only): **~$1.30** average
- Per vendor with stage B refinement: **~$2.40**
- Tagline pass (Haiku): negligible (~$0.005)
- Mirror: free at the per-vendor level (Browserless subscription)

For 50 vendors stage A + selective stage B on top 20: ~$95 total.

### Common gotchas

- `BROWSERLESS_API_KEY` must be in env for `loop` (which shells out to
  the mirror CLI). `source ~/.zshrc` before running.
- The two CLIs live in separate venvs. When `loop` shells out to
  `uxv-mirror`, pass the absolute path: `--mirror-cli
  /path/to/mirroring/.venv/bin/uxv-mirror`.
- Canonicalize is idempotent — safe to re-run after adding new round-B
  outputs.
- Tagline timeouts (Haiku API hiccups) happen occasionally. Backfill
  with `uxv-extract tagline --target-id <slug>` after the batch.

## Canonical Data

`vendors/` is the source of truth for public site generation. Each fully
canonicalized vendor directory contains:

- `profile.json` — 7-question structured profile + 100-word editorial tagline
- `products.json` — structured ProductDetail catalog (typically 5–15 products)
- `canonicalize_report.json` — merge provenance + citation drift counts
- `website/` — mirrored evidence (raw HTML, Browserless JSON, normalized text, PDFs)

## Generate The Site

Generate Astro content from canonical JSON:

```bash
cd fe/generator
uv run python -m generator
```

Build the site:

```bash
cd fe/site
npm run build
```

`npm run build` runs the generator first through the `sync-content` script.

## Archive Vendors To S3

Use the Python/boto3 archival script:

```bash
python3 scripts/archive_vendors_to_s3.py s3://your-bucket/uxv --enable-bucket-versioning
```

Install boto3 if needed:

```bash
python3 -m pip install --upgrade 'boto3[crt]'
```

The `boto3[crt]` extra matters when using `aws login` profiles such as
`uxv-archive`; AWS SDK support for console-login credentials requires AWS CRT
support.

The script creates a compressed `vendors/` archive, uploads immutable snapshot
objects, and writes a manifest containing the archive SHA256, file count,
vendor count, byte size, git commit, and S3 URI.

Before creating the multi-GB archive, the script:

- loads simple AWS-related `export AWS_...=...` assignments from `~/.zshrc`
  unless `--no-zshrc` is set
- calls STS to confirm credentials are usable
- checks whether the target bucket exists
- creates the bucket idempotently when it is missing, unless `--no-create-bucket`
  is set
- reports permission failures with the IAM action that was denied

Uploads:

- `s3://bucket/prefix/snapshots/vendors-YYYYMMDDTHHMMSSZ-<git>.tar.gz`
- `s3://bucket/prefix/snapshots/vendors-YYYYMMDDTHHMMSSZ-<git>.manifest.json`
- `s3://bucket/prefix/latest/vendors.tar.gz`
- `s3://bucket/prefix/latest/vendors.manifest.json`

S3 versioning is bucket-level. Timestamped `snapshots/` keys provide explicit
time travel. `--enable-bucket-versioning` also preserves overwritten versions
of the `latest/` objects.

Useful options:

```bash
python3 scripts/archive_vendors_to_s3.py s3://your-bucket/uxv \
  --profile your-aws-profile \
  --region us-east-1 \
  --storage-class STANDARD_IA \
  --sse AES256
```

If the bucket should already exist:

```bash
python3 scripts/archive_vendors_to_s3.py s3://your-bucket/uxv --no-create-bucket
```

Dry run:

```bash
python3 scripts/archive_vendors_to_s3.py s3://your-bucket/uxv --dry-run
```
