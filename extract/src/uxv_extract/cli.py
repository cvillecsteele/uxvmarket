"""Command-line entry point.

Usage:
    uxv-extract profile \\
        --run-id <run> --target-id <target> \\
        --workspace-root /path/to/mirroring \\
        [--out /path/to/profile.json] \\
        [--model claude-sonnet-4-6] [--max-turns 30]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .agent import run_profile_extraction
from .batch import BatchConfig, BatchExitCode, run_batch
from .canonicalize import canonicalize_vendor, write_report
from .corpus import CorpusReader
from .followups import aggregate_run_followups, write_followups_jsonl
from .loop import LoopConfig, run_loop
from .migrate import migrate_citations
from .products_agent import run_product_extraction
from .schema import ProductCatalog, Profile
from .tagline_agent import (
    DEFAULT_TAGLINE_MODEL,
    generate_tagline,
    write_tagline_into_profile,
)
from .triage_agent import DEFAULT_MAX_PRODUCTS, run_triage

DEFAULT_MODEL = "claude-sonnet-4-6"
# Profile and triage are atomic-submit (one or a few `submit_*` calls
# total). 30 turns is a generous local cap — anything beyond that is
# the agent thrashing, not progressing. Bigger limits would let a
# stuck profile pass eat the whole per-vendor wall-clock budget.
DEFAULT_MAX_TURNS = 30
# Products is incremental: each `add_product` consumes one turn.
# 15-product catalogs need 50+ turns of read-then-add cycles. 100 is a
# safe ceiling without inviting runaway.
DEFAULT_PRODUCTS_MAX_TURNS = 100
# Hard ceiling: do not raise via --max-cost-usd above this without
# explicit user approval. Past runs that allowed $5/vendor produced
# rich-corpus extractions (igus, curtiss-wright) at $1.58–$1.97 — well
# under $2.50, so this cap doesn't sacrifice quality.
DEFAULT_MAX_COST_USD_PER_VENDOR = 2.50
DEFAULT_TIMEOUT_SEC_PER_VENDOR = 600  # 10 minutes
DEFAULT_BATCH_COST_USD = 300.0
DEFAULT_BATCH_TIMEOUT_SEC = 12 * 60 * 60  # 12 hours
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_CONCURRENCY = 4
DEFAULT_MIRROR_CALLS_PER_TARGET = 15


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uxv-extract",
        description="Extract cited supplier profiles from mirrored corpora.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "profile",
        help="Extract a profile for a single mirrored target.",
    )
    p.add_argument(
        "--vendor-slug",
        help="Read the canonical corpus at <vendors_root>/<slug>/website/. "
        "Mutually exclusive with --run-id/--target-id/--workspace-root.",
    )
    p.add_argument(
        "--vendors-root",
        type=Path,
        default=None,
        help="With --vendor-slug. Default: <project-root>/vendors.",
    )
    p.add_argument("--run-id")
    p.add_argument("--target-id")
    p.add_argument("--workspace-root", type=Path)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD_PER_VENDOR,
        help=f"Per-vendor USD cap (default {DEFAULT_MAX_COST_USD_PER_VENDOR}). "
        "Set to 0 to disable.",
    )
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC_PER_VENDOR,
        help=f"Per-vendor TOTAL wall-clock cap across all passes "
        f"(profile + triage + products combined; default "
        f"{DEFAULT_TIMEOUT_SEC_PER_VENDOR}). Each pass gets "
        f"`remaining = budget - elapsed` as its own timeout. When "
        f"the budget exhausts mid-pass, that pass returns a partial "
        f"via the partial-on-cap path; no work is discarded. Set to "
        f"0 to disable.",
    )

    pr = sub.add_parser(
        "products",
        help="Run the products-pass extractor for a single mirrored target.",
    )
    pr.add_argument(
        "--vendor-slug",
        help="Read the canonical corpus at <vendors_root>/<slug>/website/. "
        "Mutually exclusive with --run-id/--target-id/--workspace-root.",
    )
    pr.add_argument(
        "--vendors-root",
        type=Path,
        default=None,
        help="With --vendor-slug. Default: <project-root>/vendors.",
    )
    pr.add_argument("--run-id")
    pr.add_argument("--target-id")
    pr.add_argument("--workspace-root", type=Path)
    pr.add_argument("--out", type=Path, default=None)
    pr.add_argument("--profile", type=Path, default=None,
                    help="Path to profile.json from the vendor pass; if "
                         "present, summarised as context. Defaults to "
                         "<package>/output/runs/<run-id>/<target-id>/profile.json.")
    pr.add_argument("--model", default=DEFAULT_MODEL)
    pr.add_argument(
        "--max-turns", type=int, default=DEFAULT_PRODUCTS_MAX_TURNS,
        help=f"Default {DEFAULT_PRODUCTS_MAX_TURNS} (incremental "
        f"products needs ~50+ turns; profile/triage subcommands use "
        f"a tighter cap of {DEFAULT_MAX_TURNS}).",
    )
    pr.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD_PER_VENDOR)
    pr.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC_PER_VENDOR)
    pr.add_argument(
        "--max-products",
        type=int,
        default=DEFAULT_MAX_PRODUCTS,
        help=f"When > 0 (default {DEFAULT_MAX_PRODUCTS}): run a triage "
        f"pre-pass that stack-ranks and truncates to this many products "
        f"before full extraction. Avoids hitting Sonnet's 32k output cap "
        f"on rich corpora. Set to 0 to disable triage.",
    )

    fu = sub.add_parser(
        "followups",
        help="Aggregate fetch_requests across a run into a JSONL "
        "suitable for the next mirror pass.",
    )
    fu.add_argument("--run-id", required=True)
    fu.add_argument("--out", type=Path, default=None)

    lp = sub.add_parser(
        "loop",
        help="Round-B flow: aggregate fetch_requests from a completed "
        "extract run, invoke `uxv-mirror` to fetch them as seed_urls, "
        "then re-extract on the new mirror run.",
    )
    lp.add_argument("--source-run-id", required=True,
                    help="Existing extract run to read fetch_requests from.")
    lp.add_argument("--new-run-id", required=True,
                    help="Run id for round B (mirror writes here; re-extract reads it).")
    lp.add_argument("--workspace-root", type=Path, required=True,
                    help="Mirroring workspace root.")
    lp.add_argument("--target-id", action="append", default=None,
                    help="Restrict to specific target_ids (repeatable). "
                    "Default: every target with fetch_requests in the source run.")
    lp.add_argument("--include-products", action="store_true",
                    help="After each pass-B profile succeeds, run pass-B products too.")
    lp.add_argument("--model", default=DEFAULT_MODEL)
    lp.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    lp.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD_PER_VENDOR)
    lp.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC_PER_VENDOR)
    lp.add_argument("--max-batch-cost-usd", type=float, default=DEFAULT_BATCH_COST_USD)
    lp.add_argument("--max-batch-runtime-sec", type=int, default=DEFAULT_BATCH_TIMEOUT_SEC)
    lp.add_argument("--max-consecutive-failures", type=int, default=DEFAULT_MAX_CONSECUTIVE_FAILURES)
    lp.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    lp.add_argument("--mirror-cli", default="uxv-mirror",
                    help="Path to the uxv-mirror executable (default: on PATH).")
    lp.add_argument("--max-mirror-calls-per-target", type=int,
                    default=DEFAULT_MIRROR_CALLS_PER_TARGET,
                    help="Per-target Browserless call cap for round-B mirror.")
    lp.add_argument(
        "--max-products",
        type=int,
        default=DEFAULT_MAX_PRODUCTS,
        help=f"With --include-products: round-B products pass runs a "
        f"triage pre-pass that stack-ranks and caps products at this "
        f"many (default {DEFAULT_MAX_PRODUCTS}). Round-B corpora are "
        f"bigger than A so the 32k output cap is more likely without "
        f"triage. Set to 0 to disable.",
    )

    b = sub.add_parser(
        "batch",
        help="Extract profiles for every target in a mirroring run, "
        "with per-vendor and aggregate guardrails.",
    )
    b.add_argument("--run-id", required=True)
    b.add_argument("--workspace-root", type=Path, required=True)
    b.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output dir; defaults to extract/output/runs/<run-id>/",
    )
    b.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="JSONL journal path; defaults to <out-dir>/batch.jsonl",
    )
    b.add_argument("--target-id", action="append", default=None,
                   help="Restrict to specific target_ids (repeatable). "
                   "Default: every target dir under the run.")
    b.add_argument("--model", default=DEFAULT_MODEL)
    b.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    b.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD_PER_VENDOR,
        help=f"Per-vendor USD cap (default {DEFAULT_MAX_COST_USD_PER_VENDOR})",
    )
    b.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC_PER_VENDOR,
        help=f"Per-vendor wall-clock cap in seconds (default "
        f"{DEFAULT_TIMEOUT_SEC_PER_VENDOR})",
    )
    b.add_argument(
        "--max-batch-cost-usd",
        type=float,
        default=DEFAULT_BATCH_COST_USD,
        help=f"Aggregate USD cap across the whole batch (default "
        f"${DEFAULT_BATCH_COST_USD})",
    )
    b.add_argument(
        "--max-batch-runtime-sec",
        type=int,
        default=DEFAULT_BATCH_TIMEOUT_SEC,
        help=f"Aggregate wall-clock cap (default "
        f"{DEFAULT_BATCH_TIMEOUT_SEC}s = 12h). Set to 0 to disable.",
    )
    b.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        help=f"Stop after the last N completed targets all failed "
        f"(rolling window; default {DEFAULT_MAX_CONSECUTIVE_FAILURES})",
    )
    b.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max concurrent extractions in flight (default "
        f"{DEFAULT_CONCURRENCY}). When the batch cost cap fires, in-flight "
        f"work completes — overshoot is bounded by "
        f"(concurrency-1) * --max-cost-usd.",
    )
    b.add_argument(
        "--include-products",
        action="store_true",
        help="After each profile pass succeeds, run the products pass for "
        "the same target. Both passes share the per-vendor caps; products "
        "cost adds to the aggregate batch cap. Products failures are "
        "isolated and do NOT count toward --max-consecutive-failures.",
    )
    b.add_argument(
        "--max-products",
        type=int,
        default=DEFAULT_MAX_PRODUCTS,
        help=f"With --include-products: run a triage pre-pass that "
        f"stack-ranks and caps products at this many before full "
        f"extraction (default {DEFAULT_MAX_PRODUCTS}). Set to 0 to "
        f"disable triage.",
    )

    mc = sub.add_parser(
        "migrate-citations",
        help="Rewrite resource_id in existing extract outputs to point at "
        "the canonical vendor corpus at vendors/<slug>/website/. Snippet "
        "drift (canonical re-fetched the page with different content) is "
        "flagged in migration_report.json; flagged citations are NOT "
        "rewritten.",
    )
    mc.add_argument("slug")
    mc.add_argument(
        "--vendors-root",
        type=Path,
        default=None,
        help="Default: <project-root>/vendors (sibling of extract/).",
    )
    mc.add_argument(
        "--extract-root",
        type=Path,
        default=None,
        help="Default: extract/ (this package's root).",
    )
    mc.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the report; do not rewrite files.",
    )

    tg = sub.add_parser(
        "tagline",
        help="Run the cheap Haiku tagline pass for one vendor. Reads "
        "profile.json + (optional) products.json + corpus, produces a "
        "≤100-word editorial tagline, writes it back into profile.json. "
        "Idempotent: skips if profile already has a tagline.",
    )
    tg.add_argument(
        "--vendor-slug",
        help="Read the canonical corpus at <vendors_root>/<slug>/website/ "
        "and update extract/output/runs/<slug>-canonical/<slug>/profile.json.",
    )
    tg.add_argument("--vendors-root", type=Path, default=None)
    tg.add_argument("--run-id")
    tg.add_argument("--target-id")
    tg.add_argument("--workspace-root", type=Path)
    tg.add_argument("--model", default=DEFAULT_TAGLINE_MODEL)
    tg.add_argument("--timeout-sec", type=float, default=60.0)
    tg.add_argument(
        "--force", action="store_true",
        help="Regenerate even if profile already has a tagline.",
    )

    cn = sub.add_parser(
        "canonicalize",
        help="Merge all per-run profile.json + products.json files for "
        "one or more vendors into vendors/<slug>/profile.json + "
        "products.json. Citations are migrated to canonical resource "
        "IDs (snippet drift annotated, not dropped). Field-merge rule: "
        "base on the oldest run, upsert field-by-field for each newer "
        "run. No agent calls.",
    )
    cn.add_argument(
        "slugs", nargs="*",
        help="Vendor slug(s) to canonicalize. If empty, processes every "
        "slug that has any extract output under "
        "extract/output/runs/*/<slug>/.",
    )
    cn.add_argument("--vendors-root", type=Path, default=None)
    cn.add_argument("--extract-root", type=Path, default=None)

    return parser


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def default_output_path(*, package_root: Path, run_id: str, target_id: str) -> Path:
    return package_root / "output" / "runs" / run_id / target_id / "profile.json"


def default_products_output_path(*, package_root: Path, run_id: str, target_id: str) -> Path:
    return package_root / "output" / "runs" / run_id / target_id / "products.json"


def _resolve_vendors_root(vendors_root: Path | None) -> Path:
    """Default <project-root>/vendors (sibling of extract/)."""
    return vendors_root or (package_root().parent / "vendors")


def _vendor_slug_run_id(slug: str) -> str:
    """Synthetic run_id for canonical-corpus extracts: `<slug>-canonical`.
    Keeps extract/output paths predictable and avoids colliding with real
    mirror run ids."""
    return f"{slug}-canonical"


def _load_corpus(
    *,
    run_id: str | None,
    target_id: str | None,
    workspace_root: Path | None,
    vendor_slug: str | None,
    vendors_root: Path | None,
) -> CorpusReader:
    if vendor_slug:
        return CorpusReader.from_vendor_canonical(
            vendors_root=_resolve_vendors_root(vendors_root), slug=vendor_slug,
        )
    return CorpusReader.from_workspace(
        workspace_root=workspace_root, run_id=run_id, target_id=target_id,
    )


def _effective_run_target(
    *, run_id: str | None, target_id: str | None, vendor_slug: str | None,
) -> tuple[str, str]:
    if vendor_slug:
        return _vendor_slug_run_id(vendor_slug), vendor_slug
    return run_id, target_id


def _validate_corpus_args(parser: argparse.ArgumentParser, args) -> None:
    """Enforce: pass `--vendor-slug` XOR (`--run-id` AND `--target-id` AND
    `--workspace-root`)."""
    has_slug = bool(getattr(args, "vendor_slug", None))
    legacy_fields = (args.run_id, args.target_id, args.workspace_root)
    has_legacy = any(legacy_fields)
    if has_slug and has_legacy:
        parser.error(
            "--vendor-slug is mutually exclusive with --run-id/--target-id/"
            "--workspace-root"
        )
    if not has_slug and not all(legacy_fields):
        parser.error(
            "must pass either --vendor-slug or all three of --run-id, "
            "--target-id, --workspace-root"
        )


async def run_products_command(
    *,
    run_id: str | None = None,
    target_id: str | None = None,
    workspace_root: Path | None = None,
    vendor_slug: str | None = None,
    vendors_root: Path | None = None,
    out: Path | None = None,
    profile_path: Path | None = None,
    model: str,
    max_turns: int,
    max_cost_usd: float | None = None,
    timeout_sec: float | None = None,
    max_products: int = 0,
) -> int:
    try:
        corpus = _load_corpus(
            run_id=run_id, target_id=target_id, workspace_root=workspace_root,
            vendor_slug=vendor_slug, vendors_root=vendors_root,
        )
    except FileNotFoundError as exc:
        print(f"corpus not found: {exc}", file=sys.stderr)
        return 2

    effective_run, effective_target = _effective_run_target(
        run_id=run_id, target_id=target_id, vendor_slug=vendor_slug,
    )
    out_path = out or default_products_output_path(
        package_root=package_root(),
        run_id=effective_run,
        target_id=effective_target,
    )
    resolved_profile = profile_path or default_output_path(
        package_root=package_root(),
        run_id=effective_run,
        target_id=effective_target,
    )

    # Per-vendor wall-clock budget: `timeout_sec` is the TOTAL across
    # triage + products, not per pass. Each pass gets `remaining =
    # budget - elapsed_so_far` so a vendor-total cap is honored.
    import time as _time
    started = _time.monotonic()

    def _remaining() -> float | None:
        if timeout_sec is None:
            return None
        return max(0.0, timeout_sec - (_time.monotonic() - started))

    # Triage is atomic-submit and rarely needs more than 30 turns;
    # use the profile/triage default cap regardless of --max-turns
    # (which controls the products pass).
    triage_max_turns = min(max_turns, DEFAULT_MAX_TURNS)

    priority_names: list[str] | None = None
    if max_products > 0:
        triage = await run_triage(
            corpus,
            model=model,
            max_turns=triage_max_turns,
            max_cost_usd=max_cost_usd,
            timeout_sec=_remaining(),
            max_products=max_products,
            profile_path=resolved_profile,
        )
        triage_path = out_path.parent / "products-priority.json"
        triage_path.parent.mkdir(parents=True, exist_ok=True)
        triage_path.write_text(triage.model_dump_json(indent=2))
        print(
            f"triage: {triage.agent_listed} candidates → "
            f"top {len(triage.products)} ({triage_path})"
        )
        priority_names = [p.name for p in triage.products]

    catalog = await run_product_extraction(
        corpus,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        timeout_sec=_remaining(),
        profile_path=resolved_profile,
        priority_names=priority_names,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(catalog.model_dump_json(indent=2))
    print(f"wrote {out_path}")
    return 0


async def run_profile_command(
    *,
    run_id: str | None = None,
    target_id: str | None = None,
    workspace_root: Path | None = None,
    vendor_slug: str | None = None,
    vendors_root: Path | None = None,
    out: Path | None = None,
    model: str,
    max_turns: int,
    max_cost_usd: float | None = None,
    timeout_sec: float | None = None,
) -> int:
    try:
        corpus = _load_corpus(
            run_id=run_id, target_id=target_id, workspace_root=workspace_root,
            vendor_slug=vendor_slug, vendors_root=vendors_root,
        )
    except FileNotFoundError as exc:
        print(f"corpus not found: {exc}", file=sys.stderr)
        return 2

    effective_run, effective_target = _effective_run_target(
        run_id=run_id, target_id=target_id, vendor_slug=vendor_slug,
    )
    out_path = out or default_output_path(
        package_root=package_root(),
        run_id=effective_run,
        target_id=effective_target,
    )

    profile = await run_profile_extraction(
        corpus,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        timeout_sec=timeout_sec,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(profile.model_dump_json(indent=2))
    print(f"wrote {out_path}")
    return 0


def run_followups_command(*, run_id: str, out: Path | None) -> int:
    run_root = package_root() / "output" / "runs" / run_id
    if not run_root.is_dir():
        print(f"no extract run dir at {run_root}", file=sys.stderr)
        return 2
    followups = aggregate_run_followups(run_root)
    out_path = out or (run_root / "followups.jsonl")
    write_followups_jsonl(followups, out_path)
    total_urls = sum(len(f.follow_ups) for f in followups)
    print(
        f"wrote {out_path} ({len(followups)} target(s), {total_urls} URL(s))"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "profile":
        _validate_corpus_args(parser, args)
        return asyncio.run(
            run_profile_command(
                run_id=args.run_id,
                target_id=args.target_id,
                workspace_root=args.workspace_root,
                vendor_slug=args.vendor_slug,
                vendors_root=args.vendors_root,
                out=args.out,
                model=args.model,
                max_turns=args.max_turns,
                max_cost_usd=args.max_cost_usd if args.max_cost_usd > 0 else None,
                timeout_sec=args.timeout_sec if args.timeout_sec > 0 else None,
            )
        )

    if args.command == "products":
        _validate_corpus_args(parser, args)
        return asyncio.run(
            run_products_command(
                run_id=args.run_id,
                target_id=args.target_id,
                workspace_root=args.workspace_root,
                vendor_slug=args.vendor_slug,
                vendors_root=args.vendors_root,
                out=args.out,
                profile_path=args.profile,
                model=args.model,
                max_turns=args.max_turns,
                max_cost_usd=args.max_cost_usd if args.max_cost_usd > 0 else None,
                timeout_sec=args.timeout_sec if args.timeout_sec > 0 else None,
                max_products=args.max_products,
            )
        )

    if args.command == "followups":
        return run_followups_command(run_id=args.run_id, out=args.out)

    if args.command == "loop":
        cfg = LoopConfig(
            source_run_id=args.source_run_id,
            new_run_id=args.new_run_id,
            workspace_root=args.workspace_root,
            extract_root=package_root(),
            target_ids=args.target_id,
            include_products=args.include_products,
            model=args.model,
            max_turns=args.max_turns,
            per_vendor_cost_usd=args.max_cost_usd,
            per_vendor_timeout_sec=args.timeout_sec,
            batch_cost_usd=args.max_batch_cost_usd,
            batch_timeout_sec=(
                args.max_batch_runtime_sec
                if args.max_batch_runtime_sec > 0 else None
            ),
            max_consecutive_failures=args.max_consecutive_failures,
            concurrency=args.concurrency,
            mirror_cli=args.mirror_cli,
            max_mirror_calls_per_target=args.max_mirror_calls_per_target,
            max_products=args.max_products,
        )
        return asyncio.run(run_loop(cfg))

    if args.command == "batch":
        return run_batch_command(args)

    if args.command == "migrate-citations":
        return run_migrate_citations_command(args)

    if args.command == "tagline":
        _validate_corpus_args(parser, args)
        return asyncio.run(run_tagline_command(args))

    if args.command == "canonicalize":
        return run_canonicalize_command(args)

    parser.error(f"unknown command: {args.command}")
    return 2


def run_canonicalize_command(args) -> int:
    extract_root = args.extract_root or package_root()
    vendors_root = args.vendors_root or (extract_root.parent / "vendors")

    slugs = args.slugs
    if not slugs:
        # Discover every slug that has at least one extract output.
        runs_root = extract_root / "output" / "runs"
        seen: set[str] = set()
        if runs_root.is_dir():
            for run_dir in runs_root.iterdir():
                if not run_dir.is_dir():
                    continue
                for d in run_dir.iterdir():
                    if d.is_dir() and (
                        (d / "profile.json").exists()
                        or (d / "products.json").exists()
                    ):
                        seen.add(d.name)
        slugs = sorted(seen)

    if not slugs:
        print("no slugs to canonicalize", file=sys.stderr)
        return 0

    profile_count = 0
    products_count = 0
    drift_total = 0
    for slug in slugs:
        report = canonicalize_vendor(
            slug, extract_root=extract_root, vendors_root=vendors_root,
        )
        write_report(report, vendors_root)
        profile_count += int(report.profile_written)
        products_count += int(report.products_written)
        drift_total += report.citations_drift
        flags = []
        if report.profile_written: flags.append("profile")
        if report.products_written: flags.append("products")
        flag_str = "+".join(flags) if flags else "—"
        print(
            f"{slug:<40} {flag_str:<20} "
            f"runs(profile={len(report.profile_runs)},products={len(report.products_runs)}) "
            f"cites(total={report.citations_total},migrated={report.citations_migrated},"
            f"drift={report.citations_drift})"
        )
    print(
        f"\n{len(slugs)} slugs processed: {profile_count} profile, "
        f"{products_count} products written. {drift_total} citations "
        f"with snippet drift (re-run mirror+extract to refresh)."
    )
    return 0


async def run_tagline_command(args) -> int:
    try:
        corpus = _load_corpus(
            run_id=args.run_id, target_id=args.target_id,
            workspace_root=args.workspace_root,
            vendor_slug=args.vendor_slug, vendors_root=args.vendors_root,
        )
    except FileNotFoundError as exc:
        print(f"corpus not found: {exc}", file=sys.stderr)
        return 2

    effective_run, effective_target = _effective_run_target(
        run_id=args.run_id, target_id=args.target_id,
        vendor_slug=args.vendor_slug,
    )
    profile_path = default_output_path(
        package_root=package_root(),
        run_id=effective_run, target_id=effective_target,
    )
    products_path = default_products_output_path(
        package_root=package_root(),
        run_id=effective_run, target_id=effective_target,
    )
    if not profile_path.exists():
        print(f"no profile.json at {profile_path}", file=sys.stderr)
        return 2

    profile = Profile.model_validate_json(profile_path.read_text())
    if profile.tagline is not None and not args.force:
        print(
            f"profile already has a tagline (use --force to regenerate): "
            f"{profile.tagline[:80]!r}"
        )
        return 0

    catalog: ProductCatalog | None = None
    if products_path.exists():
        try:
            catalog = ProductCatalog.model_validate_json(products_path.read_text())
        except Exception as exc:
            print(f"warning: products.json malformed; ignoring ({exc})", file=sys.stderr)

    tagline = await generate_tagline(
        profile=profile, catalog=catalog, corpus=corpus,
        model=args.model, timeout_sec=args.timeout_sec,
    )
    write_tagline_into_profile(profile_path, tagline)
    print(f"wrote tagline ({len(tagline.split())} words) to {profile_path}")
    return 0


def run_migrate_citations_command(args) -> int:
    extract_root = args.extract_root or package_root()
    vendors_root = args.vendors_root or (extract_root.parent / "vendors")
    try:
        report = migrate_citations(
            args.slug,
            vendors_root=vendors_root,
            extract_root=extract_root,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    total = sum(f.citations_total for f in report.files)
    migrated = sum(f.citations_migrated for f in report.files)
    flagged = sum(f.citations_flagged for f in report.files)
    print(
        f"migrate-citations: {len(report.files)} file(s), "
        f"{total} citation(s), {migrated} migrated, {flagged} flagged"
        f"{' (dry-run)' if args.dry_run else ''}"
    )
    return 0 if flagged == 0 else 1


def run_batch_command(args) -> int:
    out_dir = args.out_dir or (
        package_root() / "output" / "runs" / args.run_id
    )
    journal = args.journal or (out_dir / "batch.jsonl")
    config = BatchConfig(
        workspace_root=args.workspace_root,
        run_id=args.run_id,
        out_dir=out_dir,
        journal_path=journal,
        model=args.model,
        max_turns=args.max_turns,
        per_vendor_cost_usd=args.max_cost_usd,
        per_vendor_timeout_sec=args.timeout_sec,
        batch_cost_usd=args.max_batch_cost_usd,
        batch_timeout_sec=(
            args.max_batch_runtime_sec
            if args.max_batch_runtime_sec > 0
            else None
        ),
        max_consecutive_failures=args.max_consecutive_failures,
        target_ids=args.target_id,
        concurrency=args.concurrency,
        include_products=args.include_products,
        max_products=args.max_products,
    )
    code = asyncio.run(run_batch(config))
    print(
        f"batch finished: exit_code={int(code)} "
        f"(journal: {config.journal_path})"
    )
    return int(code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
