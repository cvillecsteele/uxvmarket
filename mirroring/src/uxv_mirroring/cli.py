from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from uxv_mirroring.browserless import MissingBrowserlessCredentials
from uxv_mirroring.contracts import CoverageMode, MirrorTarget, ProfileName
from uxv_mirroring.mirror import MirrorClient, policy_for_profile
from uxv_mirroring.materialize import slugify
from uxv_mirroring.promote import promote
from uxv_mirroring.registry import find_covered_entry, load_registry, registry_summary
from uxv_mirroring.state import load_run_state, summarize_run_state, validate_unique_targets


def parse_target(raw: str) -> MirrorTarget:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("target must be in Name=https://example.com form")
    name, url = raw.split("=", 1)
    name = name.strip()
    url = url.strip()
    if not name or not url.startswith(("http://", "https://")):
        raise argparse.ArgumentTypeError("target must include a name and http(s) URL")
    return MirrorTarget(target_id=slugify(name), display_name=name, homepage_url=url)


def parse_target_file(path: Path) -> list[MirrorTarget]:
    targets: list[MirrorTarget] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        display_name = str(payload.get("display_name") or "").strip()
        homepage_url = str(payload.get("homepage_url") or "").strip()
        if not display_name or not homepage_url:
            raise ValueError(f"{path}:{line_number}: display_name and homepage_url are required")
        seed_urls: list[str] = []
        for url in payload.get("seed_urls") or []:
            if isinstance(url, str) and url:
                seed_urls.append(url)
        for entry in payload.get("follow_ups") or []:
            if isinstance(entry, dict):
                url = entry.get("url")
                if isinstance(url, str) and url:
                    seed_urls.append(url)
        seed_urls = list(dict.fromkeys(seed_urls))
        targets.append(
            MirrorTarget(
                target_id=str(payload.get("target_id") or slugify(display_name)),
                display_name=display_name,
                homepage_url=homepage_url,
                categories=list(payload.get("categories") or []),
                notes=list(payload.get("notes") or []),
                seed_urls=seed_urls,
            )
        )
    return targets


class SignalStopper:
    def __init__(self) -> None:
        self.requested = False
        self.signum: int | None = None

    def install(self) -> None:
        def handler(signum, _frame) -> None:
            if self.requested:
                raise SystemExit(128 + signum)
            self.requested = True
            self.signum = signum
            print("stop requested; finishing current URL and checkpointing...", file=sys.stderr)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def exit_code(self) -> int:
        if self.signum is None:
            return 0
        return 128 + self.signum


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uxv-mirror")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(command) -> None:
        command.add_argument("--workspace-root", default=".", help="Workspace root for output/; defaults to current directory")
        command.add_argument("--profile", choices=["quick_evidence", "serious_vendor", "full_audit"], default="quick_evidence")
        command.add_argument("--target", action="append", type=parse_target, default=[])
        command.add_argument("--target-file", action="append", type=Path, default=[], help="JSONL target file")
        command.add_argument("--run-id", default=None, help="Optional explicit run id")
        coverage = command.add_mutually_exclusive_group()
        coverage.add_argument("--reuse-covered", action="store_const", const="reuse", dest="coverage_mode", default="reuse")
        coverage.add_argument("--skip-covered", action="store_const", const="skip", dest="coverage_mode")
        coverage.add_argument("--force", action="store_const", const="force", dest="coverage_mode")
        command.add_argument("--max-age-days", type=int, default=None)
        command.add_argument("--max-calls-per-target", type=int, default=None, help="Cap paid Browserless /map + /smart-scrape calls per target")

    mirror = sub.add_parser("mirror", help="Mirror one or more vendor targets")
    add_common(mirror)

    validate = sub.add_parser("validate", help="Run a mirror and print a compact validation report")
    add_common(validate)

    inspect = sub.add_parser("inspect-run", help="Inspect a prior run manifest")
    inspect.add_argument("run_id")
    inspect.add_argument("--workspace-root", default=".")

    resume = sub.add_parser("resume", help="Resume a paused or incomplete run")
    resume.add_argument("run_id")
    resume.add_argument("--workspace-root", default=".")
    resume.add_argument("--retry-failed", action="store_true")

    status = sub.add_parser("status", help="Show resumable run state")
    status.add_argument("run_id")
    status.add_argument("--workspace-root", default=".")

    coverage = sub.add_parser("coverage", help="Inspect target coverage registry")
    coverage.add_argument("--workspace-root", default=".")
    coverage.add_argument("--profile", choices=["quick_evidence", "serious_vendor", "full_audit"], default="quick_evidence")
    coverage.add_argument("--target", type=parse_target, default=None)
    coverage.add_argument("--max-age-days", type=int, default=None)

    prom = sub.add_parser(
        "promote",
        help="Build/refresh the canonical per-vendor evidence directory at "
        "<vendors-root>/<slug>/website/ from every per-run mirror corpus "
        "for the given slug.",
    )
    prom.add_argument("slug")
    prom.add_argument("--workspace-root", default=".",
                      help="Mirroring workspace (where output/runs/ lives). "
                      "Defaults to current directory.")
    prom.add_argument("--vendors-root", default=None,
                      help="Canonical vendor evidence root. Defaults to "
                      "<workspace-root>/../vendors/.")
    prom.add_argument("--no-auto-promote", action="store_true",
                      help="Reserved; promote is always explicit when "
                      "invoked via this subcommand.")

    return parser


def collect_targets(args) -> list[MirrorTarget]:
    targets = list(args.target or [])
    for target_file in args.target_file or []:
        targets.extend(parse_target_file(target_file))
    if not targets:
        raise ValueError("at least one --target or --target-file entry is required")
    validate_unique_targets(targets)
    return targets


def run_mirror(args) -> int:
    profile: ProfileName = args.profile
    coverage_mode: CoverageMode = args.coverage_mode
    policy = policy_for_profile(profile)
    if args.max_calls_per_target is not None:
        policy.max_browserless_calls_per_target = args.max_calls_per_target
    stopper = SignalStopper()
    stopper.install()
    workspace_root = Path(args.workspace_root).resolve()
    try:
        targets = collect_targets(args)
        corpora = MirrorClient().mirror_targets(
            targets,
            policy=policy,
            workspace_root=workspace_root,
            run_id=args.run_id,
            stop_requested=lambda: stopper.requested,
            coverage_mode=coverage_mode,
            max_age_days=args.max_age_days,
        )
    except MissingBrowserlessCredentials as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "run_id": corpora[0].run_id if corpora else None,
        "corpora": [
            {
                "target_id": corpus.target.target_id,
                "quality_status": corpus.quality_report.status,
                "manifest_path": corpus.manifest_path,
                "resource_count": len(corpus.resources),
                "browserless_calls_used": corpus.quality_report.browserless_calls_used,
                "browserless_call_budget": corpus.quality_report.browserless_call_budget,
                "budget_exhausted": corpus.quality_report.budget_exhausted,
            }
            for corpus in corpora
        ],
    }, indent=2))
    _auto_promote_for_corpora(corpora, workspace_root=workspace_root)
    return stopper.exit_code()


def _auto_promote_for_corpora(corpora, *, workspace_root: Path) -> None:
    """Auto-invoke promote() for every freshly-mirrored slug. Failures
    log to stderr but never break the mirror call."""
    if not corpora:
        return
    vendors_root = workspace_root.parent / "vendors"
    for corpus in corpora:
        slug = corpus.target.target_id
        try:
            result = promote(
                slug, workspace_root=workspace_root, vendors_root=vendors_root
            )
        except Exception as exc:
            print(
                f"warning: auto-promote failed for {slug!r}: {exc!r}",
                file=sys.stderr,
            )
            continue
        if result.action == "promoted":
            print(
                f"promoted {slug} → {result.canonical_dir} "
                f"(fetched={result.fetched_count}, new_urls={result.new_url_count})",
                file=sys.stderr,
            )


def run_validate(args) -> int:
    profile: ProfileName = args.profile
    coverage_mode: CoverageMode = args.coverage_mode
    policy = policy_for_profile(profile)
    if args.max_calls_per_target is not None:
        policy.max_browserless_calls_per_target = args.max_calls_per_target
    try:
        targets = collect_targets(args)
        corpora = MirrorClient().mirror_targets(
            targets,
            policy=policy,
            workspace_root=Path(args.workspace_root).resolve(),
            run_id=args.run_id,
            coverage_mode=coverage_mode,
            max_age_days=args.max_age_days,
        )
    except MissingBrowserlessCredentials as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps([
        {
            "target": corpus.target.display_name,
            "status": corpus.quality_report.status,
            "reasons": corpus.quality_report.reasons,
            "fetched_pages": corpus.quality_report.fetched_pages,
            "fetched_documents": corpus.quality_report.fetched_documents,
            "total_text_chars": corpus.quality_report.total_text_chars,
            "browserless_calls_used": corpus.quality_report.browserless_calls_used,
            "browserless_call_budget": corpus.quality_report.browserless_call_budget,
            "budget_exhausted": corpus.quality_report.budget_exhausted,
            "quality_report_path": corpus.quality_report_path,
        }
        for corpus in corpora
    ], indent=2))
    return 0


def run_resume(args) -> int:
    workspace_root = Path(args.workspace_root).resolve()
    stopper = SignalStopper()
    stopper.install()
    try:
        state = load_run_state(workspace_root, args.run_id)
        corpora = MirrorClient().mirror_targets(
            state.targets,
            policy=state.policy,
            workspace_root=workspace_root,
            run_id=args.run_id,
            resume=True,
            retry_failed=args.retry_failed,
            stop_requested=lambda: stopper.requested,
        )
    except FileNotFoundError:
        print(f"run state not found for {args.run_id}", file=sys.stderr)
        return 1
    except MissingBrowserlessCredentials as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "run_id": args.run_id,
        "corpora": [
            {
                "target_id": corpus.target.target_id,
                "quality_status": corpus.quality_report.status,
                "manifest_path": corpus.manifest_path,
                "resource_count": len(corpus.resources),
                "browserless_calls_used": corpus.quality_report.browserless_calls_used,
                "browserless_call_budget": corpus.quality_report.browserless_call_budget,
                "budget_exhausted": corpus.quality_report.budget_exhausted,
            }
            for corpus in corpora
        ],
    }, indent=2))
    _auto_promote_for_corpora(corpora, workspace_root=workspace_root)
    return stopper.exit_code()


def run_promote(args) -> int:
    workspace_root = Path(args.workspace_root).resolve()
    if args.vendors_root:
        vendors_root = Path(args.vendors_root).resolve()
    else:
        vendors_root = workspace_root.parent / "vendors"
    try:
        result = promote(
            args.slug, workspace_root=workspace_root, vendors_root=vendors_root
        )
    except Exception as exc:
        print(f"promote failed for {args.slug!r}: {exc!r}", file=sys.stderr)
        return 1
    print(json.dumps({
        "slug": result.slug,
        "action": result.action,
        "reason": result.reason,
        "canonical_dir": str(result.canonical_dir) if result.canonical_dir else None,
        "fetched_count": result.fetched_count,
        "new_url_count": result.new_url_count,
        "source_run_ids": result.source_run_ids,
    }, indent=2))
    return 0


def run_status(args) -> int:
    try:
        state = load_run_state(Path(args.workspace_root).resolve(), args.run_id)
    except FileNotFoundError:
        print(f"run state not found for {args.run_id}", file=sys.stderr)
        return 1
    print(json.dumps(summarize_run_state(state), indent=2, sort_keys=True))
    return 0


def run_coverage(args) -> int:
    workspace_root = Path(args.workspace_root).resolve()
    registry = load_registry(workspace_root)
    if args.target is not None:
        policy = policy_for_profile(args.profile)
        entry = find_covered_entry(registry, target=args.target, policy=policy, max_age_days=args.max_age_days)
        print(json.dumps({"covered": entry is not None, "entry": entry.model_dump() if entry else None}, indent=2, sort_keys=True))
        return 0
    print(json.dumps(registry_summary(registry), indent=2, sort_keys=True))
    return 0


def run_inspect(args) -> int:
    manifest_path = Path(args.workspace_root).resolve() / "output" / "runs" / args.run_id / "manifest.json"
    if not manifest_path.exists():
        print(f"run manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    print(manifest_path.read_text(encoding="utf-8"), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mirror":
        return run_mirror(args)
    if args.command == "promote":
        return run_promote(args)
    if args.command == "validate":
        return run_validate(args)
    if args.command == "inspect-run":
        return run_inspect(args)
    if args.command == "resume":
        return run_resume(args)
    if args.command == "status":
        return run_status(args)
    if args.command == "coverage":
        return run_coverage(args)
    parser.error(f"unknown command {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
