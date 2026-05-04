from __future__ import annotations

from pathlib import Path

from uxv_mirroring.contracts import (
    MirrorPolicy,
    MirrorTarget,
    RunState,
    TargetRunState,
    UrlRunState,
    utc_now_iso,
)
from uxv_mirroring.materialize import write_json


def run_state_path(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "output" / "runs" / run_id / "run_state.json"


def initialize_run_state(
    *,
    run_id: str,
    workspace_root: Path,
    targets: list[MirrorTarget],
    policy: MirrorPolicy,
) -> RunState:
    return RunState(
        run_id=run_id,
        workspace_root=str(workspace_root),
        profile=policy.profile,
        policy=policy,
        targets=targets,
        target_states=[
            TargetRunState(target_id=target.target_id)
            for target in targets
        ],
    )


def load_run_state(workspace_root: Path, run_id: str) -> RunState:
    path = run_state_path(workspace_root, run_id)
    return RunState.model_validate_json(path.read_text(encoding="utf-8"))


def save_run_state(workspace_root: Path, state: RunState) -> Path:
    state.updated_at = utc_now_iso()
    return write_json(run_state_path(workspace_root, state.run_id), state.model_dump())


def recover_running_work(state: RunState, *, retry_failed: bool = False) -> RunState:
    if state.status == "running":
        state.status = "paused"
        state.pause_reason = state.pause_reason or "recovered interrupted running state"
    for target_state in state.target_states:
        if target_state.status == "running":
            target_state.status = "pending"
            target_state.updated_at = utc_now_iso()
        if retry_failed and target_state.status == "failed":
            target_state.status = "pending"
            target_state.error_message = None
            target_state.updated_at = utc_now_iso()
        for url_state in target_state.urls:
            if url_state.status == "running":
                url_state.status = "pending"
                url_state.updated_at = utc_now_iso()
            if retry_failed and url_state.status == "failed":
                url_state.status = "pending"
                url_state.error_message = None
                url_state.updated_at = utc_now_iso()
    state.status = "running"
    state.current_target_id = None
    state.current_url = None
    state.pause_reason = None
    state.updated_at = utc_now_iso()
    return state


def target_state_for(state: RunState, target_id: str) -> TargetRunState:
    for target_state in state.target_states:
        if target_state.target_id == target_id:
            return target_state
    target_state = TargetRunState(target_id=target_id)
    state.target_states.append(target_state)
    return target_state


def url_state_for(target_state: TargetRunState, url: str) -> UrlRunState:
    for url_state in target_state.urls:
        if url_state.url == url:
            return url_state
    url_state = UrlRunState(url=url)
    target_state.urls.append(url_state)
    return url_state


def set_selected_urls(target_state: TargetRunState, urls: list[str]) -> None:
    if not target_state.selected_urls:
        target_state.selected_urls = urls
    known = {url_state.url for url_state in target_state.urls}
    for url in target_state.selected_urls:
        if url not in known:
            target_state.urls.append(UrlRunState(url=url))
            known.add(url)
    target_state.updated_at = utc_now_iso()


def mark_target(state: RunState, target_id: str, status: str, *, error_message: str | None = None) -> None:
    target_state = target_state_for(state, target_id)
    target_state.status = status  # type: ignore[assignment]
    target_state.error_message = error_message
    target_state.updated_at = utc_now_iso()
    state.current_target_id = target_id if status == "running" else None
    state.updated_at = utc_now_iso()


def mark_url(
    state: RunState,
    target_id: str,
    url: str,
    status: str,
    *,
    resource_id: str | None = None,
    skip_reason: str | None = None,
    error_message: str | None = None,
) -> None:
    target_state = target_state_for(state, target_id)
    url_state = url_state_for(target_state, url)
    url_state.status = status  # type: ignore[assignment]
    if resource_id is not None:
        url_state.resource_id = resource_id
    url_state.skip_reason = skip_reason
    url_state.error_message = error_message
    url_state.updated_at = utc_now_iso()
    state.current_target_id = target_id
    state.current_url = url if status == "running" else None
    state.updated_at = utc_now_iso()


def summarize_run_state(state: RunState) -> dict[str, object]:
    target_counts: dict[str, int] = {}
    url_counts: dict[str, int] = {}
    browserless_calls_used = 0
    for target_state in state.target_states:
        target_counts[target_state.status] = target_counts.get(target_state.status, 0) + 1
        browserless_calls_used += target_state.browserless_calls_used
        for url_state in target_state.urls:
            url_counts[url_state.status] = url_counts.get(url_state.status, 0) + 1
    return {
        "run_id": state.run_id,
        "status": state.status,
        "profile": state.profile,
        "current_target_id": state.current_target_id,
        "current_url": state.current_url,
        "pause_reason": state.pause_reason,
        "target_counts": target_counts,
        "url_counts": url_counts,
        "browserless_calls_used": browserless_calls_used,
        "browserless_call_budget_per_target": state.policy.max_browserless_calls_per_target,
        "updated_at": state.updated_at,
    }


def validate_unique_targets(targets: list[MirrorTarget]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for target in targets:
        if target.target_id in seen:
            duplicates.append(target.target_id)
        seen.add(target.target_id)
    if duplicates:
        names = ", ".join(sorted(set(duplicates)))
        raise ValueError(f"duplicate target_id(s): {names}")
