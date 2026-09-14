"""Durable, rate-limit-aware queue for WB weekly finance archive jobs.

A job is accepted once, its state is persisted in Yandex Object Storage, and a
worker step performs at most one real Wildberries API request. Marketplace rate
limits remain enforced by the shared MarketplaceClient/Redis controller. If a
slot is busy, the job is rescheduled instead of sleeping inside the MCP call.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .archive_yandex import YandexObjectStorageArchiveStore
from .business_registry import resolve_business_cabinet
from .rate_limit import redis_connection_kwargs, redis_url_from_env
from .tools import resolve_named_cabinet
from .wb_finance_archive import (
    ARCHIVE_CABINETS,
    DATASET,
    MAIN_REPORT_TYPE,
    ArchiveLock,
    merge_annual_csv,
    merge_registry,
    normalize_main_fragments,
    parse_csv_bytes,
    registry_complete_ids,
)
from .wb_token_rate_policy import wb_token_type

QUEUE_VERSION = 1
QUEUE_KEY = "marketplace-archive:v2:wb-finance:due"
JOB_FOLDER = ("app", "jobs", "wb-finance")
MAX_DISCOVERY_PAGE = 1000
DETAIL_PAGE_LIMIT = 5000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _boundary_start(year: int) -> date:
    first = date(year, 1, 1)
    return first - timedelta(days=first.weekday())


def _fragment_overlaps_year(fragment: Any, year: int) -> bool:
    start = date(year, 1, 1).isoformat()
    end = date(year, 12, 31).isoformat()
    return fragment.logical_week_to >= start and fragment.logical_week_from <= end


def _retry_after(payload: dict[str, Any]) -> float:
    try:
        return max(0.0, float(
            payload.get("retry_after_seconds", 0)
            or payload.get("retry_after_sec", 0)
            or 0
        ))
    except (TypeError, ValueError):
        return 0.0


def _is_rate_limited(payload: dict[str, Any]) -> bool:
    return (
        payload.get("error_type") == "rate_limit"
        or payload.get("error") in {"rate_limit", "rate_limit_busy"}
        or int(payload.get("code", 0) or 0) == 429
    )


def _fragment_dict(fragment: Any) -> dict[str, Any]:
    return {
        "report_id": int(fragment.report_id),
        "date_from": fragment.date_from,
        "date_to": fragment.date_to,
        "create_date": fragment.create_date,
        "report_type": int(fragment.report_type),
        "logical_week_from": fragment.logical_week_from,
        "logical_week_to": fragment.logical_week_to,
    }


class WBFinanceArchiveJobQueue:
    """Persistent WB archive queue; each worker step does <= 1 provider call."""

    def __init__(self, wb_module: Any, store: YandexObjectStorageArchiveStore) -> None:
        self.wb = wb_module
        self.store = store

    @staticmethod
    def normalize_cabinet(seller: str) -> str:
        normalized = str(seller).strip()
        if normalized in ARCHIVE_CABINETS:
            return normalized
        entry = resolve_business_cabinet("wb", normalized)
        if entry is None or entry.cabinet not in ARCHIVE_CABINETS:
            raise ValueError(f"Unknown WB seller/cabinet: {seller}")
        return entry.cabinet

    @staticmethod
    def job_id(cabinet: str, year: int) -> str:
        return f"wb-finance-{cabinet}-{int(year)}"

    def _resolve_creds(self, cabinet: str) -> dict[str, str]:
        business = resolve_business_cabinet("wb", cabinet)
        credential_name = business.cabinet if business else cabinet
        creds, error = resolve_named_cabinet(self.wb.client, credential_name)
        if error:
            raise RuntimeError(str(error.get("message") or error))
        assert creds is not None
        return creds

    async def _redis(self):
        url = redis_url_from_env()
        if not url:
            raise RuntimeError("Shared Redis is required for archive queue scheduling")
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
            due = time.time() + max(0.0, float(delay_seconds))
            await client.zadd(QUEUE_KEY, {job_id: due})
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
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        await self.store.upload_bytes(folder, name, payload, mime_type="application/json")

    async def _stage_location(self, job_id: str, report_id: int) -> tuple[str, str]:
        folder = await self.store.ensure_folder_path([*JOB_FOLDER, job_id, "staging"])
        return folder, f"report-{int(report_id)}.csv"

    async def _registry_location(self) -> tuple[str, str]:
        folder = await self.store.ensure_folder_path(["app", "registry"])
        return folder, "reports_registry.csv"

    async def _annual_location(self, cabinet: str, year: int) -> tuple[str, str]:
        folder = await self.store.ensure_folder_path(
            ["База данных", "WB", cabinet, str(year), "finance", "weekly", "main"]
        )
        return folder, f"{cabinet}__weekly_main__{year}.csv"

    async def enqueue(self, *, year: int, seller: str) -> dict[str, Any]:
        year = int(year)
        if year < 2024 or year > date.today().year:
            raise ValueError("year must be between 2024 and the current year")
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
                "cabinet": cabinet,
                "year": year,
            }

        creds = self._resolve_creds(cabinet)
        now = _utc_now()
        state: dict[str, Any] = {
            "version": QUEUE_VERSION,
            "job_id": job_id,
            "marketplace": "wb",
            "dataset": DATASET,
            "cabinet": cabinet,
            "year": year,
            "token_type": wb_token_type(str(creds.get("token", ""))) or "unknown",
            "status": "QUEUED",
            "phase": "DISCOVER",
            "discovery_offset": 0,
            "fragments": [],
            "report_index": 0,
            "current_rrd_id": 0,
            "provider_calls": 0,
            "completed_report_ids": [],
            "last_retry_after_seconds": 0,
            "last_error": None,
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        await self._save(state)
        await self._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "created": True,
            "status": state["status"],
            "phase": state["phase"],
            "cabinet": cabinet,
            "year": year,
            "token_type": state["token_type"],
        }

    async def status(self, job_id: str) -> dict[str, Any]:
        state = await self._load(str(job_id))
        if state is None:
            return {"ok": False, "error": "archive_job_not_found", "job_id": job_id}
        fragments = state.get("fragments") or []
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": state.get("status"),
            "phase": state.get("phase"),
            "marketplace": state.get("marketplace"),
            "dataset": state.get("dataset"),
            "cabinet": state.get("cabinet"),
            "year": state.get("year"),
            "token_type": state.get("token_type"),
            "reports_total": len(fragments),
            "report_index": int(state.get("report_index", 0) or 0),
            "completed_report_ids": state.get("completed_report_ids") or [],
            "provider_calls": int(state.get("provider_calls", 0) or 0),
            "current_rrd_id": int(state.get("current_rrd_id", 0) or 0),
            "last_retry_after_seconds": state.get("last_retry_after_seconds", 0),
            "last_error": state.get("last_error"),
            "created_at_utc": state.get("created_at_utc"),
            "updated_at_utc": state.get("updated_at_utc"),
        }

    async def worker_step(self, job_id: str = "") -> dict[str, Any]:
        selected = str(job_id).strip()
        if not selected:
            selected, wait = await self._next_due()
            if not selected:
                return {
                    "ok": True,
                    "action": "idle",
                    "retry_after_seconds": int(math.ceil(wait)) if wait > 0 else 0,
                }
        try:
            async with ArchiveLock(
                key=f"marketplace-archive:v2:wb-finance:job:{selected}",
                ttl_seconds=180,
            ):
                state = await self._load(selected)
                if state is None:
                    await self._unschedule(selected)
                    return {"ok": False, "error": "archive_job_not_found", "job_id": selected}
                if state.get("status") == "COMPLETE":
                    await self._unschedule(selected)
                    return {"ok": True, "job_id": selected, "status": "COMPLETE", "action": "noop"}
                state["status"] = "RUNNING"
                state["last_retry_after_seconds"] = 0
                state["last_error"] = None
                if state.get("phase") == "DISCOVER":
                    return await self._discover_step(state)
                if state.get("phase") == "DOWNLOAD":
                    return await self._download_step(state)
                state["status"] = "FAILED"
                state["last_error"] = f"Unknown archive job phase: {state.get('phase')}"
                await self._save(state)
                await self._unschedule(selected)
                return {"ok": False, "job_id": selected, "status": "FAILED", "error": state["last_error"]}
        except RuntimeError as exc:
            if "already running" in str(exc):
                return {
                    "ok": True,
                    "job_id": selected,
                    "status": "BUSY",
                    "action": "worker_busy",
                    "retry_after_seconds": 2,
                }
            raise

    async def _wait_or_fail(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
        *,
        action: str,
    ) -> dict[str, Any] | None:
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
            return {
                "ok": True,
                "job_id": state["job_id"],
                "status": state["status"],
                "phase": state["phase"],
                "action": "waiting_rate_limit",
                "retry_after_seconds": int(math.ceil(retry)),
                "provider_calls": state["provider_calls"],
            }
        retryable = bool(payload.get("retryable"))
        if retryable:
            retry = max(5.0, _retry_after(payload) or 15.0)
            state["status"] = "WAITING_RETRY"
            state["last_retry_after_seconds"] = retry
            state["last_error"] = str(payload.get("message") or payload.get("error") or payload)[:1000]
            await self._save(state)
            await self._schedule(str(state["job_id"]), retry)
            return {
                "ok": True,
                "job_id": state["job_id"],
                "status": state["status"],
                "phase": state["phase"],
                "action": "waiting_retry",
                "retry_after_seconds": int(math.ceil(retry)),
                "last_error": state["last_error"],
            }
        state["status"] = "FAILED"
        state["last_error"] = str(payload.get("message") or payload.get("error") or payload)[:1000]
        await self._save(state)
        await self._unschedule(str(state["job_id"]))
        return {
            "ok": False,
            "job_id": state["job_id"],
            "status": "FAILED",
            "phase": state["phase"],
            "error": state["last_error"],
            "action": action,
        }

    async def _discover_step(self, state: dict[str, Any]) -> dict[str, Any]:
        cabinet = str(state["cabinet"])
        year = int(state["year"])
        creds = self._resolve_creds(cabinet)
        spec = self.wb.catalog.get("wb_finance_sales_reports_list")
        if spec is None:
            raise RuntimeError("wb_finance_sales_reports_list contract is missing")
        end = min(date(year, 12, 31), date.today())
        result = await self.wb.client.call_spec(
            spec,
            creds_override=creds,
            json_body={
                "dateFrom": _boundary_start(year).isoformat(),
                "dateTo": end.isoformat(),
                "period": "weekly",
                "limit": MAX_DISCOVERY_PAGE,
                "offset": int(state.get("discovery_offset", 0) or 0),
            },
        )
        handled = await self._wait_or_fail(state, result, action="discover_failed")
        if handled is not None:
            return handled
        page = result.get("data") or []
        if not isinstance(page, list):
            raise RuntimeError("WB report discovery returned non-list data")
        accumulated = {
            int(item["report_id"]): dict(item)
            for item in (state.get("fragments") or [])
            if item.get("report_id")
        }
        for fragment in normalize_main_fragments(page):
            if _fragment_overlaps_year(fragment, year):
                accumulated[int(fragment.report_id)] = _fragment_dict(fragment)
        state["fragments"] = sorted(
            accumulated.values(),
            key=lambda item: (item["logical_week_from"], item["date_from"], item["report_id"]),
        )
        if len(page) >= MAX_DISCOVERY_PAGE:
            state["discovery_offset"] = int(state.get("discovery_offset", 0) or 0) + len(page)
            state["status"] = "QUEUED"
            await self._save(state)
            await self._schedule(str(state["job_id"]), 0)
            return {
                "ok": True,
                "job_id": state["job_id"],
                "status": state["status"],
                "phase": "DISCOVER",
                "action": "discovery_page_saved",
                "reports_discovered": len(state["fragments"]),
                "provider_calls": state["provider_calls"],
            }
        registry_folder, registry_name = await self._registry_location()
        _, registry_data = await self.store.download_named(registry_folder, registry_name)
        complete = registry_complete_ids(registry_data, cabinet)
        state["fragments"] = [
            item for item in state["fragments"] if int(item["report_id"]) not in complete
        ]
        state["completed_report_ids"] = sorted(complete)
        state["phase"] = "DOWNLOAD"
        state["report_index"] = 0
        state["current_rrd_id"] = 0
        if not state["fragments"]:
            state["status"] = "COMPLETE"
            await self._save(state)
            await self._unschedule(str(state["job_id"]))
            return {
                "ok": True,
                "job_id": state["job_id"],
                "status": "COMPLETE",
                "phase": "DOWNLOAD",
                "action": "nothing_missing",
                "provider_calls": state["provider_calls"],
            }
        state["status"] = "QUEUED"
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": state["status"],
            "phase": state["phase"],
            "action": "discovery_complete",
            "reports_pending": len(state["fragments"]),
            "boundary_start": _boundary_start(year).isoformat(),
            "provider_calls": state["provider_calls"],
        }

    async def _download_step(self, state: dict[str, Any]) -> dict[str, Any]:
        fragments = state.get("fragments") or []
        index = int(state.get("report_index", 0) or 0)
        if index >= len(fragments):
            state["status"] = "COMPLETE"
            await self._save(state)
            await self._unschedule(str(state["job_id"]))
            return {"ok": True, "job_id": state["job_id"], "status": "COMPLETE", "action": "complete"}
        fragment = fragments[index]
        report_id = int(fragment["report_id"])
        cabinet = str(state["cabinet"])
        creds = self._resolve_creds(cabinet)
        spec = self.wb.catalog.get("wb_finance_sales_reports_detailed_by_id")
        if spec is None:
            raise RuntimeError("wb_finance_sales_reports_detailed_by_id contract is missing")
        result = await self.wb.client.call_spec(
            spec,
            creds_override=creds,
            path_values={"reportId": report_id},
            json_body={
                "limit": DETAIL_PAGE_LIMIT,
                "rrdId": int(state.get("current_rrd_id", 0) or 0),
            },
        )
        handled = await self._wait_or_fail(state, result, action="download_failed")
        if handled is not None:
            return handled
        status_code = int(result.get("status", 0) or 0)
        page = result.get("data") or []
        if status_code == 204 or page == []:
            return await self._finalize_report(state, fragment)
        if not isinstance(page, list):
            raise RuntimeError(f"WB report {report_id} detail returned non-list data")
        for row in page:
            if int(row.get("reportId") or report_id) != report_id:
                raise RuntimeError(f"WB report {report_id} returned a foreign reportId")
            if int(row.get("reportType") or 0) != MAIN_REPORT_TYPE:
                raise RuntimeError(f"WB report {report_id} is not reportType=1")
        try:
            next_rrd = int(page[-1]["rrdId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"WB report {report_id} has no terminal rrdId") from exc
        previous_rrd = int(state.get("current_rrd_id", 0) or 0)
        if next_rrd <= previous_rrd:
            raise RuntimeError(f"WB report {report_id} rrdId did not advance")
        stage_folder, stage_name = await self._stage_location(str(state["job_id"]), report_id)
        _, stage_data = await self.store.download_named(stage_folder, stage_name)
        staged, stage_stats = merge_annual_csv(stage_data, page)
        await self.store.upload_bytes(stage_folder, stage_name, staged, mime_type="text/csv")
        state["current_rrd_id"] = next_rrd
        state["status"] = "QUEUED"
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": state["status"],
            "phase": state["phase"],
            "action": "report_page_staged",
            "report_id": report_id,
            "rrd_id": next_rrd,
            "staged_rows": stage_stats.get("total_rows", 0),
            "provider_calls": state["provider_calls"],
        }

    async def _finalize_report(self, state: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
        report_id = int(fragment["report_id"])
        cabinet = str(state["cabinet"])
        year = int(state["year"])
        stage_folder, stage_name = await self._stage_location(str(state["job_id"]), report_id)
        _, stage_data = await self.store.download_named(stage_folder, stage_name)
        _, stage_rows = parse_csv_bytes(stage_data or b"")
        annual_folder, annual_name = await self._annual_location(cabinet, year)
        _, annual_data = await self.store.download_named(annual_folder, annual_name)
        merged, stats = merge_annual_csv(annual_data, stage_rows)
        annual_obj = await self.store.upload_bytes(annual_folder, annual_name, merged, mime_type="text/csv")
        registry_folder, registry_name = await self._registry_location()
        _, registry_data = await self.store.download_named(registry_folder, registry_name)
        record = {
            "marketplace": "wb",
            "cabinet": cabinet,
            "dataset": DATASET,
            "report_type": MAIN_REPORT_TYPE,
            "report_id": report_id,
            "date_from": fragment["date_from"],
            "date_to": fragment["date_to"],
            "logical_week_from": fragment["logical_week_from"],
            "logical_week_to": fragment["logical_week_to"],
            "create_date": fragment.get("create_date", ""),
            "year": year,
            "annual_file": annual_name,
            "storage_object_key": annual_obj.id,
            "rows": len(stage_rows),
            "bytes": len(merged),
            "sha256": hashlib.sha256(merged).hexdigest(),
            "status": "COMPLETE",
            "ingested_at_utc": _utc_now(),
        }
        await self.store.upload_bytes(
            registry_folder,
            registry_name,
            merge_registry(registry_data, [record]),
            mime_type="text/csv",
        )
        completed = {int(x) for x in (state.get("completed_report_ids") or [])}
        completed.add(report_id)
        state["completed_report_ids"] = sorted(completed)
        state["report_index"] = int(state.get("report_index", 0) or 0) + 1
        state["current_rrd_id"] = 0
        if int(state["report_index"]) >= len(state.get("fragments") or []):
            state["status"] = "COMPLETE"
            await self._save(state)
            await self._unschedule(str(state["job_id"]))
        else:
            state["status"] = "QUEUED"
            await self._save(state)
            await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": state["status"],
            "phase": state["phase"],
            "action": "report_finalized",
            "report_id": report_id,
            "report_rows": len(stage_rows),
            "annual_rows": stats.get("total_rows", 0),
            "annual_bytes": len(merged),
            "reports_remaining": max(0, len(state.get("fragments") or []) - int(state["report_index"])),
            "provider_calls": state["provider_calls"],
        }
