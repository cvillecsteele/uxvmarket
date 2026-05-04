"""Round-B orchestrator: extract A → followups → mirror B → extract B.

Reads `fetch_requests` from a completed extract run (the "source" run),
aggregates them per target into a `followups.jsonl`, invokes the
sibling `uxv-mirror` CLI with `--target-file` + `--force` to fetch the
agent-requested URLs as seed_urls, then re-runs `extract batch` on the
freshly-mirrored corpora.

Why subprocess to `uxv-mirror`: the mirroring package isn't a Python
import dependency of `extract`, so we shell out to the CLI on PATH.
This keeps the two packages loosely coupled — they share the artifact
contract on disk, not Python types.

Why `--force` on the mirror call: the seed_urls are URLs the extract
agent specifically asked for after determining the original mirror was
incomplete. Coverage cache lookups would skip them.

Why we inspect the mirror manifest after subprocess success: the
mirror CLI returns exit 0 even when every target failed quality
(missing BROWSERLESS_API_KEY, network errors, etc.). Trusting the exit
code alone burns the re-extract pass on an empty corpus. We read the
per-target quality_status and surface specific reasons.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .batch import BatchConfig, BatchExitCode, run_batch
from .followups import aggregate_run_followups, write_followups_jsonl


@dataclass
class LoopConfig:
    source_run_id: str
    new_run_id: str
    workspace_root: Path
    extract_root: Path
    target_ids: list[str] | None
    include_products: bool
    # extract pass-B caps:
    model: str
    max_turns: int
    per_vendor_cost_usd: float
    per_vendor_timeout_sec: float
    batch_cost_usd: float
    batch_timeout_sec: float | None
    max_consecutive_failures: int
    concurrency: int
    # mirror caps:
    mirror_cli: str
    max_mirror_calls_per_target: int
    # round-B products triage:
    max_products: int = 0
    """When > 0 AND `include_products` is True: round-B products pass
    runs a triage pre-pass that stack-ranks products and caps at this
    many before full extraction. Round-B corpora are larger than
    round-A (more URLs fetched), so the 32k output cap is more likely
    to bite without triage. Defaults to 0 = no triage (legacy)."""


SubprocessRunner = "subprocess.run"


def _inspect_mirror_b_results(
    workspace_root: Path, new_run_id: str
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Returns (succeeded_target_ids, failed_target_id_with_reasons).

    A target counts as succeeded if its quality_status is anything OTHER
    than 'failed' — `complete`, `partial`, and `review_required` all
    produce at least some text the re-extract can use.
    """
    manifest_path = (
        workspace_root / "output" / "runs" / new_run_id / "manifest.json"
    )
    if not manifest_path.exists():
        return [], []
    manifest = json.loads(manifest_path.read_text())
    succeeded: list[str] = []
    failed: list[tuple[str, list[str]]] = []
    for c in manifest.get("corpora", []):
        tid = c["target_id"]
        if c.get("quality_status") == "failed":
            reasons: list[str] = []
            qr_path = c.get("quality_report_path")
            if qr_path and Path(qr_path).exists():
                try:
                    qr = json.loads(Path(qr_path).read_text())
                    reasons = list(qr.get("reasons", []))
                except (OSError, json.JSONDecodeError):
                    pass
            failed.append((tid, reasons))
        else:
            succeeded.append(tid)
    return succeeded, failed


async def run_loop(
    config: LoopConfig,
    *,
    subprocess_run=subprocess.run,
    extract_fn=None,
    products_extract_fn=None,
) -> int:
    """Returns the exit code of the pass-B batch (or a SETUP_ERROR code
    if aggregation/mirroring fail)."""
    source_root = config.extract_root / "output" / "runs" / config.source_run_id
    if not source_root.is_dir():
        print(
            f"source extract run not found: {source_root}",
            file=sys.stderr,
        )
        return int(BatchExitCode.SETUP_ERROR)

    followups = aggregate_run_followups(source_root)
    if config.target_ids:
        wanted = set(config.target_ids)
        followups = [f for f in followups if f.target_id in wanted]

    if not followups:
        print(
            f"no fetch_requests in any profile.json under {source_root} "
            "(nothing to do)",
            file=sys.stderr,
        )
        return int(BatchExitCode.SUCCESS)

    total_urls = sum(len(f.follow_ups) for f in followups)
    print(
        f"loop: source={config.source_run_id} → new={config.new_run_id}\n"
        f"  {len(followups)} target(s), {total_urls} URL(s) to mirror"
    )

    followups_path = source_root / f"followups-for-{config.new_run_id}.jsonl"
    write_followups_jsonl(followups, followups_path)
    print(f"  wrote {followups_path}")

    mirror_cmd = [
        config.mirror_cli, "mirror",
        "--target-file", str(followups_path),
        "--workspace-root", str(config.workspace_root),
        "--run-id", config.new_run_id,
        "--max-calls-per-target", str(config.max_mirror_calls_per_target),
        "--force",
    ]
    print(f"  → {' '.join(mirror_cmd)}")
    proc = subprocess_run(mirror_cmd)
    if proc.returncode != 0:
        print(
            f"mirroring exited {proc.returncode}; aborting before re-extract",
            file=sys.stderr,
        )
        return int(BatchExitCode.SETUP_ERROR)

    # Mirror exits 0 even when every target failed quality (missing API
    # key, network failures, etc.). Inspect each target's quality_status
    # and only re-extract the ones that actually got fetched pages.
    # Intersect with the followup target list (mirror manifest may
    # contain stale entries from prior runs at the same run_id).
    requested = {f.target_id for f in followups}
    all_succeeded, all_failed = _inspect_mirror_b_results(
        config.workspace_root, config.new_run_id
    )
    succeeded = [t for t in all_succeeded if t in requested]
    failed = [(t, r) for (t, r) in all_failed if t in requested]
    if failed:
        print(
            f"\n  mirror reported quality_status=failed for "
            f"{len(failed)} of {len(failed) + len(succeeded)} target(s):",
            file=sys.stderr,
        )
        for tid, reasons in failed:
            joined = "; ".join(reasons[:3]) if reasons else "(no reasons recorded)"
            print(f"    {tid}: {joined}", file=sys.stderr)
    if not succeeded:
        print(
            "\n  no targets produced fetched pages; aborting before "
            "re-extract.\n  (common cause: BROWSERLESS_API_KEY not set in "
            "the subprocess environment — set it in the shell that runs "
            "`uxv-extract loop`.)",
            file=sys.stderr,
        )
        return int(BatchExitCode.SETUP_ERROR)

    new_target_ids = succeeded
    out_dir = config.extract_root / "output" / "runs" / config.new_run_id
    journal_path = out_dir / "batch.jsonl"
    batch_cfg = BatchConfig(
        workspace_root=config.workspace_root,
        run_id=config.new_run_id,
        out_dir=out_dir,
        journal_path=journal_path,
        model=config.model,
        max_turns=config.max_turns,
        per_vendor_cost_usd=config.per_vendor_cost_usd,
        per_vendor_timeout_sec=config.per_vendor_timeout_sec,
        batch_cost_usd=config.batch_cost_usd,
        batch_timeout_sec=config.batch_timeout_sec,
        max_consecutive_failures=config.max_consecutive_failures,
        target_ids=new_target_ids,
        concurrency=config.concurrency,
        include_products=config.include_products,
        max_products=config.max_products,
    )
    print(f"  → re-extract batch on {len(new_target_ids)} target(s)")
    code = await run_batch(
        batch_cfg,
        extract_fn=extract_fn,
        products_extract_fn=products_extract_fn,
    )
    return int(code)
