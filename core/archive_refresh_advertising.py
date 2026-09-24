"""Coverage-aware planning for reusable WB Advertising Archive refresh cycles.

The advertising ingestion queue is intentionally responsible for bounded provider
requests, while ``dataset_coverage_registry.csv`` is the durable proof that a
specific request was already committed to canonical storage.  Reopening a
COMPLETE annual job must therefore avoid replaying stable old history while
still re-reading a short recent correction window for mutable statistics.

This module installs coverage-aware PLAN handlers on the advertising queue.  A
planned provider request is skipped only when every dataset produced by that
request has COMPLETE/PASS coverage for the exact operation/date/scope identity.
Current observation snapshots (campaign roster/info) are still refreshed every
cycle because they represent current state rather than immutable history.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .archive_coverage import complete_request_keys, request_key
from .wb_advertising_archive import coverage_registry_location
from .wb_advertising_archive_queue import (
    CLUSTER_PERIOD_DAYS,
    WBAdvertisingArchiveJobQueue,
    _chunks,
    closed_history_period,
)
from .wb_advertising_normalize import (
    plan_fullstats_requests,
    plan_period_requests,
    split_date_range,
)

ADVERTISING_CORRECTION_WINDOW_DAYS = 7


def split_refresh_period(
    date_from: str,
    date_to: str,
    *,
    yesterday: date | None = None,
    window_days: int = ADVERTISING_CORRECTION_WINDOW_DAYS,
) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """Split a closed-history range into stable history and recent corrections."""
    start = date.fromisoformat(str(date_from)[:10])
    end = date.fromisoformat(str(date_to)[:10])
    if end < start:
        raise ValueError("date_to must not precede date_from")
    closed_end = min(end, yesterday or _moscow_yesterday())
    floor = closed_end - timedelta(days=max(1, int(window_days)) - 1)
    recent_start = max(start, floor)
    stable_end = recent_start - timedelta(days=1)

    stable = (
        (start.isoformat(), stable_end.isoformat())
        if start <= stable_end
        else None
    )
    recent = (
        (recent_start.isoformat(), closed_end.isoformat())
        if recent_start <= closed_end
        else None
    )
    return stable, recent


def _request_coverage_key(*, cabinet: str, dataset: str, request: dict[str, Any]) -> str:
    return request_key(
        marketplace="wb",
        cabinet=cabinet,
        dataset=dataset,
        operation_id=str(request.get("operation_id") or ""),
        date_from=str(request.get("date_from") or ""),
        date_to=str(request.get("date_to") or ""),
        scope=request.get("scope") or {},
    )


def _moscow_yesterday() -> date:
    return datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)


def request_requires_correction_refresh(
    request: dict[str, Any],
    *,
    yesterday: date | None = None,
    window_days: int = ADVERTISING_CORRECTION_WINDOW_DAYS,
) -> bool:
    """Re-fetch recently closed mutable statistics even when coverage exists.

    WB advertising statistics can be corrected after the first observation.
    This is an MCP refresh policy, not a provider retention guarantee: the most
    recent closed days are deliberately re-read and upserted by stable key.
    Older COMPLETE coverage remains reusable.
    """
    if str(request.get("kind") or "") not in {
        "fullstats", "expenses", "payments", "search_clusters",
    }:
        return False
    try:
        request_end = date.fromisoformat(str(request.get("date_to") or "")[:10])
    except ValueError:
        return False
    closed_end = yesterday or _moscow_yesterday()
    if request_end > closed_end:
        return False
    floor = closed_end - timedelta(days=max(1, int(window_days)) - 1)
    return request_end >= floor


def request_fully_covered(
    *,
    cabinet: str,
    request: dict[str, Any],
    complete_by_dataset: dict[str, set[str]],
) -> bool:
    """Return True only when every dataset output of a request is committed.

    Snapshot requests are deliberately never suppressed.  They are observations
    of mutable current campaign state and should be refreshed on every cycle.
    """
    if str(request.get("kind") or "") == "campaign_info":
        return False
    datasets = tuple(str(item) for item in request.get("datasets") or () if str(item))
    if not datasets:
        return False
    return all(
        _request_coverage_key(cabinet=cabinet, dataset=dataset, request=request)
        in complete_by_dataset.get(dataset, set())
        for dataset in datasets
    )


async def _complete_keys_for_plan(
    queue: WBAdvertisingArchiveJobQueue,
    *,
    cabinet: str,
    datasets: Iterable[str],
) -> dict[str, set[str]]:
    parts, name = coverage_registry_location()
    parent = await queue.store.ensure_folder_path(parts)
    _, raw = await queue.store.download_named(parent, name)
    return {
        dataset: complete_request_keys(
            raw,
            marketplace="wb",
            cabinet=cabinet,
            dataset=dataset,
        )
        for dataset in sorted({str(item) for item in datasets if str(item)})
    }


async def _filter_plan(
    queue: WBAdvertisingArchiveJobQueue,
    *,
    cabinet: str,
    plan: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    datasets = {
        str(dataset)
        for request in plan
        for dataset in request.get("datasets") or ()
        if str(dataset)
    }
    complete = await _complete_keys_for_plan(queue, cabinet=cabinet, datasets=datasets)
    pending = [
        request
        for request in plan
        if request_requires_correction_refresh(request)
        or not request_fully_covered(
            cabinet=cabinet,
            request=request,
            complete_by_dataset=complete,
        )
    ]
    return pending, len(plan) - len(pending)


async def _coverage_aware_plan_step(
    self: WBAdvertisingArchiveJobQueue,
    state: dict[str, Any],
) -> dict[str, Any]:
    start, end = closed_history_period(int(state["year"]))
    all_ids = [int(value) for value in state.get("campaign_ids") or []]
    fullstats_ids = [int(value) for value in state.get("fullstats_campaign_ids") or []]
    plan: list[dict[str, Any]] = []

    # Current campaign information is an observation snapshot, so it is always
    # refreshed even if an identical bounded identity was covered earlier today.
    for ids in _chunks(all_ids, 50):
        plan.append({
            "kind": "campaign_info",
            "operation_id": "wb_get_api_advert_adverts",
            "datasets": ["ads_campaign_snapshots"],
            "date_from": start,
            "date_to": end,
            "scope": {"campaign_ids": ids},
            "query": {"ids": ",".join(str(value) for value in ids)},
        })

    stable_period, correction_period = split_refresh_period(start, end)
    fullstats_periods = [
        period for period in (stable_period, correction_period) if period is not None
    ]
    for period_start, period_end in fullstats_periods:
        for item in plan_fullstats_requests(fullstats_ids, period_start, period_end):
            ids = list(item["campaign_ids"])
            plan.append({
                "kind": "fullstats",
                "operation_id": "wb_get_adv_fullstats",
                "datasets": ["ads_campaign_daily", "ads_product_daily"],
                "date_from": item["date_from"],
                "date_to": item["date_to"],
                "scope": {"campaign_ids": ids},
                "query": {
                    "ids": ",".join(str(value) for value in ids),
                    "beginDate": item["date_from"],
                    "endDate": item["date_to"],
                },
            })

    for operation_id, dataset in (
        ("wb_get_adv_upd", "ads_expenses"),
        ("wb_get_adv_payments", "ads_payments"),
    ):
        for period_start, period_end in fullstats_periods:
            for item in plan_period_requests(operation_id, period_start, period_end):
                plan.append({
                    "kind": "expenses" if dataset == "ads_expenses" else "payments",
                    "operation_id": operation_id,
                    "datasets": [dataset],
                    "date_from": item["date_from"],
                    "date_to": item["date_to"],
                    "scope": {},
                    "query": {"from": item["date_from"], "to": item["date_to"]},
                })

    pending, skipped = await _filter_plan(
        self,
        cabinet=str(state["cabinet"]),
        plan=plan,
    )
    state["fetch_plan"] = pending
    state["fetch_index"] = 0
    state["phase"] = "FETCH"
    state["status"] = "QUEUED"
    state["coverage_skipped_base_requests"] = skipped
    await self._save(state)
    await self._schedule(str(state["job_id"]), 0)
    return {
        "ok": True,
        "job_id": state["job_id"],
        "action": "base_plan_ready",
        "provider_requests": len(pending),
        "coverage_skipped_requests": skipped,
        "candidate_requests": len(plan),
    }


async def _coverage_aware_plan_clusters_step(
    self: WBAdvertisingArchiveJobQueue,
    state: dict[str, Any],
) -> dict[str, Any]:
    product_rows = await self._read_stage_rows(str(state["job_id"]), "ads_product_daily")
    pairs = sorted({
        (int(row.get("campaign_id") or 0), int(row.get("nm_id") or 0))
        for row in product_rows
        if int(row.get("campaign_id") or 0) > 0 and int(row.get("nm_id") or 0) > 0
    })
    start_date, end_date = closed_history_period(int(state["year"]))
    stable_period, correction_period = split_refresh_period(start_date, end_date)
    periods: list[tuple[str, str]] = []
    for period in (stable_period, correction_period):
        if period is None:
            continue
        periods.extend(split_date_range(period[0], period[1], max_days=CLUSTER_PERIOD_DAYS))
    plan: list[dict[str, Any]] = []
    for start, end in periods:
        for chunk in _chunks(pairs, 100):
            items = [{"advertId": advert_id, "nmId": nm_id} for advert_id, nm_id in chunk]
            plan.append({
                "kind": "search_clusters",
                "operation_id": "wb_post_adv_normquery_stats_v1",
                "datasets": ["ads_search_cluster_daily"],
                "date_from": start,
                "date_to": end,
                "scope": {"campaign_product_pairs": [f"{a}:{n}" for a, n in chunk]},
                "json_body": {"from": start, "to": end, "items": items},
            })

    pending, skipped = await _filter_plan(
        self,
        cabinet=str(state["cabinet"]),
        plan=plan,
    )
    state["cluster_plan"] = pending
    state["cluster_index"] = 0
    state["coverage_skipped_cluster_requests"] = skipped
    if not pending:
        return await self._mark_ready_to_commit(state)
    state["phase"] = "FETCH_CLUSTERS"
    state["status"] = "QUEUED"
    await self._save(state)
    await self._schedule(str(state["job_id"]), 0)
    return {
        "ok": True,
        "job_id": state["job_id"],
        "action": "cluster_plan_ready",
        "provider_requests": len(pending),
        "coverage_skipped_requests": skipped,
        "candidate_requests": len(plan),
        "campaign_product_pairs": len(pairs),
    }


def install_advertising_coverage_planner() -> None:
    """Install the coverage-aware planner once for every queue instance."""
    if getattr(WBAdvertisingArchiveJobQueue, "_coverage_planner_v1_installed", False):
        return
    WBAdvertisingArchiveJobQueue._plan_step = _coverage_aware_plan_step  # type: ignore[method-assign]
    WBAdvertisingArchiveJobQueue._plan_clusters_step = _coverage_aware_plan_clusters_step  # type: ignore[method-assign]
    WBAdvertisingArchiveJobQueue._coverage_planner_v1_installed = True  # type: ignore[attr-defined]
