"""Durable, rate-aware ingestion queue for WB Advertising Archive V1.

One worker step performs at most one WB Promotion API request. Provider data is
normalized into durable Yandex job staging; canonical Drive publication and
coverage are handled later by the verified advertising archive worker.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .archive_coverage import request_key
from .rate_limit import redis_connection_kwargs, redis_url_from_env
from .tools import resolve_named_cabinet
from .wb_advertising import _resolve_ads_creds
from .wb_advertising_archive import (
    ARCHIVE_CABINETS,
    eligible_fullstats_campaign_ids,
    merge_annual_dataset,
    normalize_campaign_info_snapshot,
    normalize_campaign_roster,
    normalize_expenses,
    normalize_payments,
    parse_csv,
)
from .wb_advertising_normalize import (
    campaign_product_ids,
    enrich_product_attribution,
    normalize_fullstats,
    normalize_product_identity_snapshot,
    normalize_search_cluster_daily,
    plan_fullstats_requests,
    plan_period_requests,
    split_date_range,
)
from .wb_finance_archive import ArchiveLock

QUEUE_VERSION = 1
QUEUE_KEY = "marketplace-archive:v3:wb-advertising:due"
JOB_FOLDER = ("app", "jobs", "wb-advertising")
CLUSTER_PERIOD_DAYS = 31
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _moscow_today() -> date:
    return datetime.now(MOSCOW_TZ).date()


def closed_history_period(year: int) -> tuple[str, str]:
    """Return only closed calendar days; current year ends yesterday Moscow."""
    year = int(year)
    today = _moscow_today()
    if year < 2024 or year > today.year:
        raise ValueError("year must be between 2024 and the current year")
    start = date(year, 1, 1)
    end = date(year, 12, 31) if year < today.year else today - timedelta(days=1)
    if end < start:
        raise ValueError("current year has no closed advertising days yet")
    return start.isoformat(), end.isoformat()


def _retry_after(payload: dict[str, Any]) -> float:
    try:
        return max(0.0, float(payload.get("retry_after_seconds", 0) or payload.get("retry_after_sec", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _is_rate_limited(payload: dict[str, Any]) -> bool:
    return (
        payload.get("error_type") == "rate_limit"
        or payload.get("error") in {"rate_limit", "rate_limit_busy"}
        or int(payload.get("code", 0) or 0) == 429
    )


def _chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _request_lock_key(cabinet: str, request: dict[str, Any]) -> str:
    operation_id = str(request.get("operation_id") or "unknown")
    dataset = "+".join(request.get("datasets") or [request.get("dataset") or "unknown"])
    date_from = str(request.get("date_from") or "na")
    date_to = str(request.get("date_to") or "na")
    scope = request.get("scope") or {}
    identity = request_key(
        marketplace="wb",
        cabinet=cabinet,
        dataset=dataset,
        operation_id=operation_id,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
    )
    return f"marketplace-archive:v3:wb-ads:{cabinet}:{dataset}:{date_from}:{date_to}:{identity[:20]}"


class WBAdvertisingArchiveJobQueue:
    def __init__(self, wb_module: Any, store: Any) -> None:
        self.wb = wb_module
        self.store = store

    @staticmethod
    def normalize_cabinet(seller: str) -> str:
        value = str(seller).strip()
        if value not in ARCHIVE_CABINETS:
            raise ValueError(f"Unknown WB advertising cabinet: {seller}")
        return value

    @staticmethod
    def job_id(cabinet: str, year: int) -> str:
        return f"wb-advertising-{cabinet}-{int(year)}"

    async def _redis(self):
        url = redis_url_from_env()
        if not url:
            raise RuntimeError("Shared Redis is required for advertising archive scheduling")
        import redis.asyncio as redis_async
        return redis_async.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
            **redis_connection_kwargs(url),
        )

    async def _schedule(self, job_id: str, delay_seconds: float = 0.0) -> None:
        client = await self._redis()
        try:
            await client.zadd(QUEUE_KEY, {job_id: time.time() + max(0.0, float(delay_seconds))})
        finally:
            await client.aclose()

    async def _unschedule(self, job_id: str) -> None:
        client = await self._redis()
        try:
            await client.zrem(QUEUE_KEY, job_id)
        finally:
            await client.aclose()

    async def _next_due(self) -> tuple[str | None, float]:
        client = await self._redis()
        try:
            now = time.time()
            due = await client.zrangebyscore(QUEUE_KEY, "-inf", now, start=0, num=1)
            if due:
                return str(due[0]), 0.0
            upcoming = await client.zrange(QUEUE_KEY, 0, 0, withscores=True)
            if not upcoming:
                return None, 0.0
            return None, max(0.0, float(upcoming[0][1]) - now)
        finally:
            await client.aclose()

    async def _job_location(self, job_id: str) -> tuple[str, str]:
        folder = await self.store.ensure_folder_path(JOB_FOLDER)
        return folder, f"{job_id}.json"

    async def _load(self, job_id: str) -> dict[str, Any] | None:
        folder, name = await self._job_location(job_id)
        item, data = await self.store.download_named(folder, name)
        if item is None or data is None:
            return None
        return json.loads(data.decode("utf-8"))

    async def _save(self, state: dict[str, Any]) -> None:
        state["updated_at_utc"] = _utc_now()
        folder, name = await self._job_location(str(state["job_id"]))
        raw = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        await self.store.upload_bytes(folder, name, raw, mime_type="application/json")

    async def _stage_location(self, job_id: str, dataset: str) -> tuple[str, str]:
        folder = await self.store.ensure_folder_path([*JOB_FOLDER, job_id, "staging"])
        return folder, f"{dataset}.csv"

    async def _merge_stage(self, job_id: str, dataset: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        folder, name = await self._stage_location(job_id, dataset)
        _, existing = await self.store.download_named(folder, name)
        merged, stats = merge_annual_dataset(dataset, existing, rows)
        if merged:
            await self.store.upload_bytes(folder, name, merged, mime_type="text/csv")
        return stats

    async def _read_stage_rows(self, job_id: str, dataset: str) -> list[dict[str, str]]:
        folder, name = await self._stage_location(job_id, dataset)
        _, raw = await self.store.download_named(folder, name)
        _, rows = parse_csv(raw)
        return rows

    def _resolve_creds(self, cabinet: str) -> dict[str, str]:
        creds, error, _ = _resolve_ads_creds(self.wb, cabinet)
        if error or not creds:
            raise RuntimeError(str((error or {}).get("message") or error or "WB Promotion credentials unavailable"))
        return creds

    def _resolve_content_creds(self, cabinet: str) -> dict[str, str]:
        creds, error = resolve_named_cabinet(self.wb.client, cabinet)
        if error or not creds or not creds.get("token"):
            raise RuntimeError(
                str((error or {}).get("message") if isinstance(error, dict) else error)
                or f"WB content credential unavailable for {cabinet}"
            )
        return {"token": str(creds["token"])}

    async def _provider_call(
        self,
        operation_id: str,
        creds: dict[str, str],
        *,
        query: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        spec = self.wb.catalog.get(operation_id)
        if spec is None:
            return {
                "ok": False,
                "error": "contract_missing",
                "message": f"WB Promotion contract {operation_id} is missing",
                "retryable": False,
            }
        return await self.wb.client.call_spec(
            spec,
            query=query,
            json_body=json_body,
            creds_override=creds,
        )

    async def enqueue(self, *, year: int, seller: str) -> dict[str, Any]:
        year = int(year)
        closed_history_period(year)
        cabinet = self.normalize_cabinet(seller)
        job_id = self.job_id(cabinet, year)
        existing = await self._load(job_id)
        if existing is not None:
            if existing.get("status") != "COMPLETE":
                await self._schedule(job_id, 0)
            return {
                "ok": True,
                "job_id": job_id,
                "created": False,
                "status": existing.get("status"),
                "phase": existing.get("phase"),
            }
        self._resolve_creds(cabinet)
        now = _utc_now()
        state = {
            "version": QUEUE_VERSION,
            "job_id": job_id,
            "marketplace": "wb",
            "dataset_family": "advertising",
            "cabinet": cabinet,
            "year": year,
            "status": "QUEUED",
            "phase": "DISCOVER",
            "provider_calls": 0,
            "fetch_plan": [],
            "fetch_index": 0,
            "identity_plan": [],
            "identity_index": 0,
            "cluster_plan": [],
            "cluster_index": 0,
            "completed_requests": [],
            "staged_datasets": {},
            "last_retry_after_seconds": 0,
            "last_error": None,
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        await self._save(state)
        await self._schedule(job_id, 0)
        return {"ok": True, "job_id": job_id, "created": True, "status": "QUEUED", "phase": "DISCOVER"}

    async def status(self, job_id: str) -> dict[str, Any]:
        state = await self._load(str(job_id))
        if state is None:
            return {"ok": False, "error": "advertising_archive_job_not_found", "job_id": job_id}
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": state.get("status"),
            "phase": state.get("phase"),
            "cabinet": state.get("cabinet"),
            "year": state.get("year"),
            "provider_calls": int(state.get("provider_calls", 0) or 0),
            "fetch_progress": [int(state.get("fetch_index", 0) or 0), len(state.get("fetch_plan") or [])],
            "identity_progress": [int(state.get("identity_index", 0) or 0), len(state.get("identity_plan") or [])],
            "cluster_progress": [int(state.get("cluster_index", 0) or 0), len(state.get("cluster_plan") or [])],
            "staged_datasets": state.get("staged_datasets") or {},
            "commit": state.get("commit") or {},
            "last_retry_after_seconds": state.get("last_retry_after_seconds", 0),
            "last_error": state.get("last_error"),
            "updated_at_utc": state.get("updated_at_utc"),
        }

    async def worker_step(self, job_id: str = "") -> dict[str, Any]:
        selected = str(job_id).strip()
        if not selected:
            selected, wait = await self._next_due()
            if not selected:
                return {"ok": True, "action": "idle", "retry_after_seconds": int(math.ceil(wait)) if wait > 0 else 0}
        state = await self._load(selected)
        if state is None:
            await self._unschedule(selected)
            return {"ok": False, "error": "advertising_archive_job_not_found", "job_id": selected}
        if state.get("status") == "COMPLETE":
            await self._unschedule(selected)
            return {"ok": True, "job_id": selected, "status": "COMPLETE", "action": "noop"}
        if state.get("status") in {"READY_TO_COMMIT", "COMMITTING", "PROMOTION_PENDING", "WAITING_COMMIT_RETRY", "READY_TO_FINALIZE"}:
            return {"ok": True, "job_id": selected, "status": state.get("status"), "action": "commit_phase_required"}

        phase = str(state.get("phase") or "")
        cabinet = str(state["cabinet"])
        start, end = closed_history_period(int(state["year"]))
        if phase == "DISCOVER":
            request = {
                "operation_id": "wb_get_adv_promotion_count",
                "dataset": "ads_campaign_roster_snapshots",
                "date_from": start,
                "date_to": end,
                "scope": {},
            }
        elif phase == "FETCH":
            plan = state.get("fetch_plan") or []
            index = int(state.get("fetch_index", 0) or 0)
            if index >= len(plan):
                state["phase"] = "PLAN_IDENTITIES"
                state["status"] = "QUEUED"
                await self._save(state)
                await self._schedule(selected, 0)
                return {"ok": True, "job_id": selected, "action": "base_fetch_complete", "phase": "PLAN_IDENTITIES"}
            request = dict(plan[index])
        elif phase == "FETCH_IDENTITIES":
            plan = state.get("identity_plan") or []
            index = int(state.get("identity_index", 0) or 0)
            if index >= len(plan):
                state["phase"] = "ENRICH_PRODUCTS"
                state["status"] = "QUEUED"
                await self._save(state)
                await self._schedule(selected, 0)
                return {"ok": True, "job_id": selected, "action": "identity_fetch_complete", "phase": "ENRICH_PRODUCTS"}
            request = dict(plan[index])
        elif phase == "FETCH_CLUSTERS":
            plan = state.get("cluster_plan") or []
            index = int(state.get("cluster_index", 0) or 0)
            if index >= len(plan):
                return await self._mark_ready_to_commit(state)
            request = dict(plan[index])
        elif phase == "PLAN":
            return await self._plan_step(state)
        elif phase == "PLAN_IDENTITIES":
            return await self._plan_identities_step(state)
        elif phase == "ENRICH_PRODUCTS":
            return await self._enrich_products_step(state)
        elif phase == "PLAN_CLUSTERS":
            return await self._plan_clusters_step(state)
        else:
            state["status"] = "FAILED"
            state["last_error"] = f"Unknown advertising archive phase: {phase}"
            await self._save(state)
            await self._unschedule(selected)
            return {"ok": False, "job_id": selected, "status": "FAILED", "error": state["last_error"]}

        try:
            async with ArchiveLock(key=_request_lock_key(cabinet, request), ttl_seconds=180):
                state = await self._load(selected) or state
                phase = str(state.get("phase") or phase)
                if phase == "DISCOVER":
                    return await self._discover_step(state)
                if phase == "FETCH":
                    return await self._fetch_step(state, cluster=False)
                if phase == "FETCH_IDENTITIES":
                    return await self._fetch_identity_step(state)
                if phase == "FETCH_CLUSTERS":
                    return await self._fetch_step(state, cluster=True)
                state["status"] = "QUEUED"
                await self._save(state)
                await self._schedule(selected, 0)
                return {"ok": True, "job_id": selected, "action": "phase_advanced_while_waiting", "phase": phase}
        except RuntimeError as exc:
            if "already running" in str(exc):
                return {"ok": True, "job_id": selected, "status": "BUSY", "action": "resource_busy", "retry_after_seconds": 2}
            raise

    async def _wait_or_fail(self, state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("ok") is True:
            state["provider_calls"] = int(state.get("provider_calls", 0) or 0) + 1
            return None
        if _is_rate_limited(payload):
            retry = max(1.0, _retry_after(payload) or 1.0)
            state["status"] = "WAITING_RATE_LIMIT"
            state["last_retry_after_seconds"] = retry
            state["last_error"] = None
            await self._save(state)
            await self._schedule(str(state["job_id"]), retry)
            return {"ok": True, "job_id": state["job_id"], "status": state["status"], "action": "waiting_rate_limit", "retry_after_seconds": int(math.ceil(retry))}
        if bool(payload.get("retryable")):
            retry = max(5.0, _retry_after(payload) or 15.0)
            state["status"] = "WAITING_RETRY"
            state["last_retry_after_seconds"] = retry
            state["last_error"] = str(payload.get("message") or payload.get("error") or payload)[:1000]
            await self._save(state)
            await self._schedule(str(state["job_id"]), retry)
            return {"ok": True, "job_id": state["job_id"], "status": state["status"], "action": "waiting_retry", "retry_after_seconds": int(math.ceil(retry))}
        state["status"] = "FAILED"
        state["last_error"] = str(payload.get("message") or payload.get("error") or payload)[:1000]
        await self._save(state)
        await self._unschedule(str(state["job_id"]))
        return {"ok": False, "job_id": state["job_id"], "status": "FAILED", "error": state["last_error"]}

    async def _discover_step(self, state: dict[str, Any]) -> dict[str, Any]:
        cabinet = str(state["cabinet"])
        creds = self._resolve_creds(cabinet)
        payload = await self._provider_call("wb_get_adv_promotion_count", creds)
        waiting = await self._wait_or_fail(state, payload)
        if waiting is not None:
            return waiting
        observed_at = _utc_now()
        roster = normalize_campaign_roster(payload.get("data"), observed_at=observed_at)
        stats = await self._merge_stage(str(state["job_id"]), "ads_campaign_roster_snapshots", roster)
        state["campaign_ids"] = sorted({int(row["campaign_id"]) for row in roster})
        state["fullstats_campaign_ids"] = eligible_fullstats_campaign_ids(roster)
        state["staged_datasets"]["ads_campaign_roster_snapshots"] = stats
        start, end = closed_history_period(int(state["year"]))
        state["completed_requests"].append({
            "operation_id": "wb_get_adv_promotion_count",
            "datasets": ["ads_campaign_roster_snapshots"],
            "date_from": start,
            "date_to": end,
            "scope": {},
            "observed_at": observed_at,
        })
        state["phase"] = "PLAN"
        state["status"] = "QUEUED"
        state["last_error"] = None
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {"ok": True, "job_id": state["job_id"], "action": "campaign_roster_staged", "campaigns": len(roster), "fullstats_campaigns": len(state["fullstats_campaign_ids"])}

    async def _plan_step(self, state: dict[str, Any]) -> dict[str, Any]:
        start, end = closed_history_period(int(state["year"]))
        all_ids = [int(value) for value in state.get("campaign_ids") or []]
        fullstats_ids = [int(value) for value in state.get("fullstats_campaign_ids") or []]
        plan: list[dict[str, Any]] = []
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
        for item in plan_fullstats_requests(fullstats_ids, start, end):
            ids = list(item["campaign_ids"])
            plan.append({
                "kind": "fullstats",
                "operation_id": "wb_get_adv_fullstats",
                "datasets": ["ads_campaign_daily", "ads_product_daily"],
                "date_from": item["date_from"],
                "date_to": item["date_to"],
                "scope": {"campaign_ids": ids},
                "query": {"ids": ",".join(str(value) for value in ids), "beginDate": item["date_from"], "endDate": item["date_to"]},
            })
        for operation_id, dataset in (("wb_get_adv_upd", "ads_expenses"), ("wb_get_adv_payments", "ads_payments")):
            for item in plan_period_requests(operation_id, start, end):
                plan.append({
                    "kind": "expenses" if dataset == "ads_expenses" else "payments",
                    "operation_id": operation_id,
                    "datasets": [dataset],
                    "date_from": item["date_from"],
                    "date_to": item["date_to"],
                    "scope": {},
                    "query": {"from": item["date_from"], "to": item["date_to"]},
                })
        state["fetch_plan"] = plan
        state["fetch_index"] = 0
        state["phase"] = "FETCH"
        state["status"] = "QUEUED"
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {"ok": True, "job_id": state["job_id"], "action": "base_plan_ready", "provider_requests": len(plan)}

    async def _fetch_step(self, state: dict[str, Any], *, cluster: bool) -> dict[str, Any]:
        plan_name = "cluster_plan" if cluster else "fetch_plan"
        index_name = "cluster_index" if cluster else "fetch_index"
        plan = state.get(plan_name) or []
        index = int(state.get(index_name, 0) or 0)
        if index >= len(plan):
            if cluster:
                return await self._mark_ready_to_commit(state)
            state["phase"] = "PLAN_IDENTITIES"
            state["status"] = "QUEUED"
            await self._save(state)
            await self._schedule(str(state["job_id"]), 0)
            return {"ok": True, "job_id": state["job_id"], "action": "base_fetch_complete"}
        request = dict(plan[index])
        creds = self._resolve_creds(str(state["cabinet"]))
        payload = await self._provider_call(str(request["operation_id"]), creds, query=request.get("query"), json_body=request.get("json_body"))
        waiting = await self._wait_or_fail(state, payload)
        if waiting is not None:
            return waiting
        kind = str(request.get("kind") or "")
        staged: dict[str, Any] = {}
        observed_at = _utc_now()
        if kind == "campaign_info":
            rows = normalize_campaign_info_snapshot(payload.get("data"), observed_at=observed_at)
            staged["ads_campaign_snapshots"] = await self._merge_stage(str(state["job_id"]), "ads_campaign_snapshots", rows)
        elif kind == "fullstats":
            normalized = normalize_fullstats(payload.get("data"))
            for dataset in ("ads_campaign_daily", "ads_product_daily"):
                staged[dataset] = await self._merge_stage(str(state["job_id"]), dataset, normalized[dataset])
        elif kind == "expenses":
            rows = normalize_expenses(payload.get("data"), request_date_from=request["date_from"], request_date_to=request["date_to"])
            staged["ads_expenses"] = await self._merge_stage(str(state["job_id"]), "ads_expenses", rows)
        elif kind == "payments":
            rows = normalize_payments(payload.get("data"), request_date_from=request["date_from"], request_date_to=request["date_to"])
            staged["ads_payments"] = await self._merge_stage(str(state["job_id"]), "ads_payments", rows)
        elif kind == "search_clusters":
            snapshots = await self._read_stage_rows(str(state["job_id"]), "ads_campaign_snapshots")
            payment = {
                int(row.get("campaign_id") or 0): str(row.get("payment_type") or "").lower()
                for row in snapshots
                if int(row.get("campaign_id") or 0) > 0 and row.get("payment_type")
            }
            rows = normalize_search_cluster_daily(payload.get("data"), payment_type_by_campaign=payment)
            staged["ads_search_cluster_daily"] = await self._merge_stage(str(state["job_id"]), "ads_search_cluster_daily", rows)
        else:
            raise RuntimeError(f"Unsupported advertising fetch kind: {kind}")
        state["staged_datasets"].update(staged)
        state["completed_requests"].append({
            "operation_id": request["operation_id"],
            "datasets": request.get("datasets") or [],
            "date_from": request.get("date_from"),
            "date_to": request.get("date_to"),
            "scope": request.get("scope") or {},
            "observed_at": observed_at if kind == "campaign_info" else None,
        })
        state[index_name] = index + 1
        state["status"] = "QUEUED"
        state["last_retry_after_seconds"] = 0
        state["last_error"] = None
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "action": "provider_request_staged",
            "operation_id": request["operation_id"],
            "request_index": index + 1,
            "request_total": len(plan),
            "datasets": list(staged),
        }

    async def _plan_identities_step(self, state: dict[str, Any]) -> dict[str, Any]:
        product_rows = await self._read_stage_rows(str(state["job_id"]), "ads_product_daily")
        nm_ids = sorted({
            int(row.get("nm_id") or 0)
            for row in product_rows
            if int(row.get("nm_id") or 0) > 0
        })
        start, end = closed_history_period(int(state["year"]))
        plan = [
            {
                "kind": "product_identity",
                "operation_id": "wb_content_cards_list",
                "datasets": ["ads_product_identity_snapshots"],
                "date_from": start,
                "date_to": end,
                "scope": {"nm_id": nm_id},
                "json_body": {
                    "settings": {
                        "sort": {"ascending": False},
                        "filter": {"textSearch": str(nm_id), "withPhoto": -1},
                        "cursor": {"limit": 100},
                    }
                },
            }
            for nm_id in nm_ids
        ]
        state["identity_plan"] = plan
        state["identity_index"] = 0
        if not plan:
            state["phase"] = "ENRICH_PRODUCTS"
            state["status"] = "QUEUED"
            await self._save(state)
            await self._schedule(str(state["job_id"]), 0)
            return {"ok": True, "job_id": state["job_id"], "action": "identity_plan_empty"}
        state["phase"] = "FETCH_IDENTITIES"
        state["status"] = "QUEUED"
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "action": "identity_plan_ready",
            "provider_requests": len(plan),
            "nm_ids": len(nm_ids),
        }

    async def _fetch_identity_step(self, state: dict[str, Any]) -> dict[str, Any]:
        plan = state.get("identity_plan") or []
        index = int(state.get("identity_index", 0) or 0)
        if index >= len(plan):
            state["phase"] = "ENRICH_PRODUCTS"
            state["status"] = "QUEUED"
            await self._save(state)
            await self._schedule(str(state["job_id"]), 0)
            return {"ok": True, "job_id": state["job_id"], "action": "identity_fetch_complete"}

        request = dict(plan[index])
        creds = self._resolve_content_creds(str(state["cabinet"]))
        payload = await self._provider_call(
            str(request["operation_id"]),
            creds,
            query=request.get("query"),
            json_body=request.get("json_body"),
        )
        waiting = await self._wait_or_fail(state, payload)
        if waiting is not None:
            return waiting

        observed_at = _utc_now()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        expected_nm = int((request.get("scope") or {}).get("nm_id") or 0)
        cards = [
            row for row in (data.get("cards") or [])
            if isinstance(row, dict)
            and int(row.get("nmID") or row.get("nmId") or 0) == expected_nm
        ]
        rows = normalize_product_identity_snapshot({"cards": cards}, observed_at=observed_at)
        if not rows:
            state["status"] = "FAILED"
            state["last_error"] = f"WB Content API did not resolve nm_id={expected_nm}"
            await self._save(state)
            await self._unschedule(str(state["job_id"]))
            return {
                "ok": False,
                "job_id": state["job_id"],
                "status": "FAILED",
                "error": state["last_error"],
            }

        stats = await self._merge_stage(
            str(state["job_id"]), "ads_product_identity_snapshots", rows
        )
        state["staged_datasets"]["ads_product_identity_snapshots"] = stats
        state["completed_requests"].append({
            "operation_id": request["operation_id"],
            "datasets": ["ads_product_identity_snapshots"],
            "date_from": request["date_from"],
            "date_to": request["date_to"],
            "scope": request.get("scope") or {},
            "observed_at": observed_at,
        })
        state["identity_index"] = index + 1
        state["status"] = "QUEUED"
        state["last_retry_after_seconds"] = 0
        state["last_error"] = None
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "action": "product_identity_staged",
            "nm_id": expected_nm,
            "request_index": index + 1,
            "request_total": len(plan),
        }

    async def _enrich_products_step(self, state: dict[str, Any]) -> dict[str, Any]:
        job_id = str(state["job_id"])
        product_rows = await self._read_stage_rows(job_id, "ads_product_daily")
        campaign_rows = await self._read_stage_rows(job_id, "ads_campaign_snapshots")
        identity_rows = await self._read_stage_rows(job_id, "ads_product_identity_snapshots")

        advertised: dict[int, set[int]] = {}
        observed_times: list[str] = []
        for row in campaign_rows:
            campaign_id = int(row.get("campaign_id") or 0)
            if campaign_id <= 0:
                continue
            raw = row.get("campaign_nm_ids") or "[]"
            try:
                values = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                values = []
            advertised[campaign_id] = {
                int(value) for value in (values or []) if int(value) > 0
            }
            if row.get("observed_at"):
                observed_times.append(str(row["observed_at"]))

        imt_by_nm: dict[int, int | None] = {}
        for row in identity_rows:
            nm_id = int(row.get("nm_id") or 0)
            if nm_id <= 0:
                continue
            raw_imt = row.get("imt_id")
            imt_by_nm[nm_id] = int(raw_imt) if raw_imt not in (None, "", "0", 0) else None
            if row.get("observed_at"):
                observed_times.append(str(row["observed_at"]))

        observed_at = max(observed_times) if observed_times else _utc_now()
        enriched = enrich_product_attribution(
            product_rows,
            advertised_nm_ids_by_campaign=advertised,
            imt_id_by_nm=imt_by_nm,
            observed_at=observed_at,
        )
        stats = await self._merge_stage(job_id, "ads_product_daily", enriched)
        state["staged_datasets"]["ads_product_daily"] = stats
        state["phase"] = "PLAN_CLUSTERS"
        state["status"] = "QUEUED"
        state["last_error"] = None
        await self._save(state)
        await self._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "action": "product_attribution_enriched",
            "rows": len(enriched),
            "observed_at": observed_at,
        }

    async def _plan_clusters_step(self, state: dict[str, Any]) -> dict[str, Any]:
        product_rows = await self._read_stage_rows(str(state["job_id"]), "ads_product_daily")
        pairs = sorted({
            (int(row.get("campaign_id") or 0), int(row.get("nm_id") or 0))
            for row in product_rows
            if int(row.get("campaign_id") or 0) > 0
            and int(row.get("nm_id") or 0) > 0
            and (
                int(float(row.get("views") or 0)) > 0
                or int(float(row.get("clicks") or 0)) > 0
                or float(row.get("spend") or 0) > 0
                or str(row.get("conversion_type_current") or "") == "direct"
            )
        })
        start_date, end_date = closed_history_period(int(state["year"]))
        periods = split_date_range(start_date, end_date, max_days=CLUSTER_PERIOD_DAYS)
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
        state["cluster_plan"] = plan
        state["cluster_index"] = 0
        if not plan:
            return await self._mark_ready_to_commit(state)
        state["phase"] = "FETCH_CLUSTERS"
        state["status"] = "QUEUED"
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {"ok": True, "job_id": state["job_id"], "action": "cluster_plan_ready", "provider_requests": len(plan), "campaign_product_pairs": len(pairs)}

    async def _mark_ready_to_commit(self, state: dict[str, Any]) -> dict[str, Any]:
        state["phase"] = "COMMIT"
        state["status"] = "READY_TO_COMMIT"
        state["last_error"] = None
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": "READY_TO_COMMIT",
            "action": "advertising_ingestion_staged",
            "provider_calls": int(state.get("provider_calls", 0) or 0),
            "staged_datasets": sorted((state.get("staged_datasets") or {}).keys()),
            "next_required_phase": "verified canonical Drive commit + coverage COMMIT",
        }
