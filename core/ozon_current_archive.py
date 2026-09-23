"""Durable Ozon CURRENT archive refresh.

The open month is intentionally refetched as a complete snapshot on every
refresh. Ozon order status, cancellation and financial fields can change after
first observation, so CURRENT does not use a guessed overlap window.

Google Drive is canonical. Queue/staging durability is provided by the archive
store's REMOTE-local backend; publication is deterministic and idempotent.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .paginate import fetch_all as fetch_all_pages
from .rate_limit import redis_connection_kwargs, redis_url_from_env
from .tools import (
    has_proven_quota,
    has_proven_read_semantics,
    resolve_named_cabinet,
)
from .business_registry import resolve_business_cabinet
from .wb_finance_archive import ArchiveLock

OZON_ARCHIVE_CABINETS = (
    "ozon_laser_master",
    "ozon_novokshenov",
    "ozon_dmitrieva",
)

DATASETS = {
    "ozon_current_orders_fbo": {
        "operation_id": "ozon_fbo_list",
        "stable_key": ("posting_number",),
        "filename_token": "orders_fbo",
    },
    "ozon_current_orders_fbs": {
        "operation_id": "ozon_fbs_list",
        "stable_key": ("posting_number",),
        "filename_token": "orders_fbs",
    },
    "ozon_current_accruals": {
        "operation_id": "ozon_finance_accrual_by_day",
        "stable_key": ("accrual_id",),
        "filename_token": "accruals",
    },
}

QUEUE_KEY = "marketplace-archive:v2:ozon-current:due"
JOB_FOLDER = ("app", "jobs", "ozon-current")
MOSCOW = ZoneInfo("Europe/Moscow")
POSTING_LIMIT = 100
MAX_ITEMS = 100_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_period(today: date | None = None) -> tuple[date, date]:
    end = today or datetime.now(MOSCOW).date()
    return end.replace(day=1), end


def month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def provider_utc_window(start: date, end: date) -> tuple[str, str]:
    """Convert Moscow calendar-day boundaries to Ozon UTC timestamps."""
    if end < start:
        raise ValueError("end date must be on or after start date")
    start_local = datetime(
        start.year,
        start.month,
        start.day,
        0,
        0,
        0,
        tzinfo=MOSCOW,
    )
    end_local = datetime(
        end.year,
        end.month,
        end.day,
        23,
        59,
        59,
        tzinfo=MOSCOW,
    )
    start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return start_utc, end_utc


def canonical_location(
    cabinet: str,
    year: int,
    month: int,
    dataset: str,
) -> tuple[list[str], str]:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown Ozon CURRENT dataset: {dataset}")
    period = f"{int(year):04d}-{int(month):02d}"
    token = DATASETS[dataset]["filename_token"]
    return (
        ["База данных", "Ozon", cabinet, str(int(year)), "CURRENT", period],
        f"{cabinet}__{token}__{period}.csv",
    )


def coverage_location(cabinet: str, year: int, month: int) -> tuple[list[str], str]:
    period = f"{int(year):04d}-{int(month):02d}"
    return (
        ["База данных", "Ozon", cabinet, str(int(year)), "CURRENT", period],
        "current_coverage_registry.csv",
    )


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def serialize_snapshot(
    rows: list[dict[str, Any]],
    *,
    stable_key: tuple[str, ...],
) -> bytes:
    """Serialize one provider snapshot losslessly enough for future schema drift."""
    normalized = [dict(row) for row in rows if isinstance(row, dict)]
    seen: set[tuple[str, ...]] = set()
    for row in normalized:
        key = tuple(str(row.get(field) or "").strip() for field in stable_key)
        if not all(key):
            raise ValueError(f"incomplete stable key {stable_key}")
        if key in seen:
            raise ValueError(f"duplicate stable key {stable_key}: {key}")
        seen.add(key)

    normalized.sort(
        key=lambda row: tuple(str(row.get(field) or "") for field in stable_key)
    )
    discovered = {
        str(key)
        for row in normalized
        for key in row.keys()
        if str(key) not in stable_key
    }
    fields = [*stable_key, *sorted(discovered), "_raw_json"]

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        delimiter=";",
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in normalized:
        record = {field: _cell(row.get(field)) for field in fields if field != "_raw_json"}
        record["_raw_json"] = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        writer.writerow(record)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def parse_snapshot(raw: bytes | None) -> tuple[list[str], list[dict[str, str]]]:
    if not raw:
        return [], []
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    return list(reader.fieldnames or []), [dict(row) for row in reader]


def stable_key_quality(
    rows: list[dict[str, Any]],
    stable_key: tuple[str, ...],
) -> dict[str, int]:
    seen: set[tuple[str, ...]] = set()
    duplicate = 0
    incomplete = 0
    for row in rows:
        key = tuple(str(row.get(field) or "").strip() for field in stable_key)
        if not all(key):
            incomplete += 1
            continue
        if key in seen:
            duplicate += 1
        else:
            seen.add(key)
    return {
        "rows": len(rows),
        "unique_stable_keys": len(seen),
        "duplicate_stable_key_rows": duplicate,
        "incomplete_stable_key_rows": incomplete,
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parse_json_bytes(raw: bytes | None) -> Any:
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _retry_after(payload: dict[str, Any]) -> float:
    try:
        return max(
            0.0,
            float(
                payload.get("retry_after_seconds", 0)
                or payload.get("retry_after_sec", 0)
                or 0
            ),
        )
    except (TypeError, ValueError):
        return 0.0


def _is_rate_limited(payload: dict[str, Any]) -> bool:
    return (
        payload.get("error_type") == "rate_limit"
        or payload.get("error") in {"rate_limit", "rate_limit_busy"}
        or int(payload.get("code", 0) or 0) == 429
    )


class OzonCurrentArchiveJobQueue:
    """Persistent open-month Ozon snapshot queue."""

    def __init__(self, ozon_module: Any, store: Any) -> None:
        self.ozon = ozon_module
        self.store = store

    @staticmethod
    def normalize_cabinet(seller: str) -> str:
        value = str(seller).strip()
        if value in OZON_ARCHIVE_CABINETS:
            return value
        entry = resolve_business_cabinet("ozon", value)
        if entry is None or entry.cabinet not in OZON_ARCHIVE_CABINETS:
            raise ValueError(f"Unknown Ozon seller/cabinet: {seller}")
        return entry.cabinet

    @staticmethod
    def job_id(cabinet: str, period: str) -> str:
        return f"ozon-current-{cabinet}-{period}"

    def _resolve_creds(self, cabinet: str) -> dict[str, str]:
        creds, error = resolve_named_cabinet(self.ozon.client, cabinet)
        if error:
            raise RuntimeError(str(error.get("message") or error))
        assert creds is not None
        return creds

    def _spec(self, operation_id: str):
        spec = self.ozon.catalog.get(operation_id)
        if spec is None:
            raise RuntimeError(f"{operation_id} contract is missing")
        if not has_proven_read_semantics(spec):
            raise RuntimeError(f"{operation_id} read semantics are not proven")
        if not has_proven_quota(spec):
            raise RuntimeError(f"{operation_id} quota contract is not proven")
        return spec

    async def _redis(self):
        url = redis_url_from_env()
        if not url:
            raise RuntimeError("Shared Redis is required for Ozon archive scheduling")
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
            await client.zadd(
                QUEUE_KEY,
                {job_id: time.time() + max(0.0, float(delay_seconds))},
            )
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

    async def _job_parent(self, job_id: str) -> str:
        return await self.store.ensure_folder_path([*JOB_FOLDER, job_id])

    async def _job_location(self, job_id: str) -> tuple[str, str]:
        parent = await self.store.ensure_folder_path(JOB_FOLDER)
        return parent, f"{job_id}.json"

    async def _load(self, job_id: str) -> dict[str, Any] | None:
        parent, name = await self._job_location(job_id)
        item, raw = await self.store.download_named(parent, name)
        if item is None or raw is None:
            return None
        value = _parse_json_bytes(raw)
        return value if isinstance(value, dict) else None

    async def _save(self, state: dict[str, Any]) -> None:
        state["updated_at_utc"] = _utc_now()
        parent, name = await self._job_location(str(state["job_id"]))
        await self.store.upload_bytes(
            parent,
            name,
            json.dumps(
                state,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
            mime_type="application/json",
        )

    async def _stage_write(self, job_id: str, name: str, value: Any) -> None:
        parent = await self.store.ensure_folder_path([*JOB_FOLDER, job_id, "staging"])
        await self.store.upload_bytes(
            parent,
            name,
            _json_bytes(value),
            mime_type="application/json",
        )

    async def _stage_read(self, job_id: str, name: str) -> Any:
        parent = await self.store.ensure_folder_path([*JOB_FOLDER, job_id, "staging"])
        item, raw = await self.store.download_named(parent, name)
        if item is None or raw is None:
            raise RuntimeError(f"Ozon CURRENT staging file missing: {name}")
        return _parse_json_bytes(raw)

    async def enqueue(self, *, year: int, seller: str) -> dict[str, Any]:
        start, end = current_period()
        if int(year) != end.year:
            raise ValueError(
                f"Ozon CURRENT is the open month only; year must be {end.year}"
            )
        cabinet = self.normalize_cabinet(seller)
        period = month_key(end)
        job_id = self.job_id(cabinet, period)
        existing = await self._load(job_id)

        generation = 1
        action = "created"
        if existing is not None and existing.get("status") != "COMPLETE":
            await self._schedule(job_id, 0)
            return {
                "ok": True,
                "job_id": job_id,
                "created": False,
                "reopened": False,
                "refresh_action": "resumed_existing",
                "scheduled": True,
                "status": existing.get("status"),
                "phase": existing.get("phase"),
                "cabinet": cabinet,
                "year": existing.get("year"),
                "period": existing.get("period"),
                "refresh_generation": int(existing.get("refresh_generation", 1) or 1),
            }
        if existing is not None:
            generation = int(existing.get("refresh_generation", 1) or 1) + 1
            action = "reopened_complete"

        state = {
            "schema_version": 1,
            "job_id": job_id,
            "marketplace": "ozon",
            "dataset_family": "ozon_current",
            "cabinet": cabinet,
            "year": end.year,
            "month": end.month,
            "period": period,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "status": "QUEUED",
            "phase": "FETCH_FBO",
            "accrual_day": start.isoformat(),
            "provider_calls": 0,
            "refresh_generation": generation,
            "created_at_utc": (
                existing.get("created_at_utc")
                if existing
                else _utc_now()
            ),
            "refresh_started_at_utc": _utc_now(),
            "last_error": None,
            "last_retry_after_seconds": 0,
            "datasets": {},
        }
        await self._save(state)
        await self._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "created": existing is None,
            "reopened": existing is not None,
            "refresh_action": action,
            "scheduled": True,
            "status": "QUEUED",
            "phase": "FETCH_FBO",
            "cabinet": cabinet,
            "year": end.year,
            "period": period,
            "refresh_generation": generation,
        }

    async def status(self, job_id: str) -> dict[str, Any]:
        state = await self._load(str(job_id))
        if state is None:
            return {"ok": False, "error": "archive_job_not_found", "job_id": job_id}
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": state.get("status"),
            "phase": state.get("phase"),
            "cabinet": state.get("cabinet"),
            "year": state.get("year"),
            "period": state.get("period"),
            "date_from": state.get("date_from"),
            "date_to": state.get("date_to"),
            "accrual_day": state.get("accrual_day"),
            "provider_calls": int(state.get("provider_calls", 0) or 0),
            "datasets": state.get("datasets") or {},
            "refresh_generation": int(state.get("refresh_generation", 1) or 1),
            "last_retry_after_seconds": state.get("last_retry_after_seconds", 0),
            "last_error": state.get("last_error"),
            "created_at_utc": state.get("created_at_utc"),
            "updated_at_utc": state.get("updated_at_utc"),
        }

    async def _wait_or_fail(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
        *,
        action: str,
    ) -> dict[str, Any] | None:
        if payload.get("ok") is True:
            state["provider_calls"] = int(state.get("provider_calls", 0) or 0) + int(
                payload.get("pages_fetched", 1) or 1
            )
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
                "action": action + "_waiting_rate_limit",
                "retry_after_seconds": retry,
            }

        state["status"] = "FAILED"
        state["last_error"] = json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        )[:2000]
        await self._save(state)
        await self._unschedule(str(state["job_id"]))
        return {
            "ok": False,
            "job_id": state["job_id"],
            "status": "FAILED",
            "phase": state["phase"],
            "action": action + "_failed",
            "error": state["last_error"],
        }

    async def _fetch_posting_dataset(
        self,
        state: dict[str, Any],
        *,
        dataset: str,
    ) -> dict[str, Any]:
        cfg = DATASETS[dataset]
        spec = self._spec(str(cfg["operation_id"]))
        creds = self._resolve_creds(str(state["cabinet"]))
        start = date.fromisoformat(str(state["date_from"]))
        end = date.fromisoformat(str(state["date_to"]))
        since, to = provider_utc_window(start, end)
        body = {
            "filter": {
                "since": since,
                "to": to,
            },
            "limit": POSTING_LIMIT,
            "with": {
                "analytics_data": True,
                "financial_data": True,
                "legal_info": True,
            },
        }
        payload = await fetch_all_pages(
            self.ozon.client,
            spec,
            base_body=body,
            limit=POSTING_LIMIT,
            max_items=MAX_ITEMS,
            creds_override=creds,
        )
        blocked = await self._wait_or_fail(state, payload, action=dataset)
        if blocked is not None:
            return blocked

        if bool(payload.get("truncated")):
            raise RuntimeError(f"{dataset} exceeded MAX_ITEMS={MAX_ITEMS}")
        rows = payload.get("items") or []
        if not isinstance(rows, list):
            raise RuntimeError(f"{dataset} returned non-list items")
        quality = stable_key_quality(rows, tuple(cfg["stable_key"]))
        if (
            quality["duplicate_stable_key_rows"]
            or quality["incomplete_stable_key_rows"]
        ):
            raise RuntimeError(f"{dataset} failed stable-key validation: {quality}")

        await self._stage_write(str(state["job_id"]), dataset + ".json", rows)
        state["datasets"][dataset] = {
            **quality,
            "pages_fetched": int(payload.get("pages_fetched", 0) or 0),
        }
        state["phase"] = (
            "FETCH_FBS"
            if dataset == "ozon_current_orders_fbo"
            else "FETCH_ACCRUALS"
        )
        state["status"] = "QUEUED"
        state["last_error"] = None
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": "QUEUED",
            "phase": state["phase"],
            "action": dataset + "_staged",
            **quality,
        }

    async def _fetch_accrual_day(self, state: dict[str, Any]) -> dict[str, Any]:
        current = date.fromisoformat(str(state["accrual_day"]))
        end = date.fromisoformat(str(state["date_to"]))
        if current > end:
            state["phase"] = "PUBLISH"
            state["status"] = "QUEUED"
            await self._save(state)
            await self._schedule(str(state["job_id"]), 0)
            return {
                "ok": True,
                "job_id": state["job_id"],
                "status": "QUEUED",
                "phase": "PUBLISH",
                "action": "accrual_days_complete",
            }

        cfg = DATASETS["ozon_current_accruals"]
        spec = self._spec(str(cfg["operation_id"]))
        creds = self._resolve_creds(str(state["cabinet"]))
        payload = await fetch_all_pages(
            self.ozon.client,
            spec,
            base_body={"date": current.isoformat(), "last_id": ""},
            max_items=MAX_ITEMS,
            creds_override=creds,
        )
        blocked = await self._wait_or_fail(state, payload, action="ozon_current_accruals")
        if blocked is not None:
            return blocked

        if bool(payload.get("truncated")):
            raise RuntimeError(f"accruals {current.isoformat()} exceeded MAX_ITEMS={MAX_ITEMS}")
        rows = payload.get("items") or []
        if not isinstance(rows, list):
            raise RuntimeError("ozon_current_accruals returned non-list items")
        quality = stable_key_quality(rows, tuple(cfg["stable_key"]))
        if (
            quality["duplicate_stable_key_rows"]
            or quality["incomplete_stable_key_rows"]
        ):
            raise RuntimeError(
                f"accruals {current.isoformat()} failed stable-key validation: {quality}"
            )

        await self._stage_write(
            str(state["job_id"]),
            "accrual-" + current.isoformat() + ".json",
            rows,
        )
        summary = state["datasets"].setdefault(
            "ozon_current_accruals",
            {
                "rows": 0,
                "unique_stable_keys": 0,
                "duplicate_stable_key_rows": 0,
                "incomplete_stable_key_rows": 0,
                "pages_fetched": 0,
                "days_fetched": 0,
            },
        )
        summary["rows"] = int(summary.get("rows", 0)) + len(rows)
        summary["unique_stable_keys"] = int(summary.get("unique_stable_keys", 0)) + quality["unique_stable_keys"]
        summary["pages_fetched"] = int(summary.get("pages_fetched", 0)) + int(
            payload.get("pages_fetched", 0) or 0
        )
        summary["days_fetched"] = int(summary.get("days_fetched", 0)) + 1

        next_day = current + timedelta(days=1)
        state["accrual_day"] = next_day.isoformat()
        state["phase"] = "FETCH_ACCRUALS" if next_day <= end else "PUBLISH"
        state["status"] = "QUEUED"
        state["last_error"] = None
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": "QUEUED",
            "phase": state["phase"],
            "action": "accrual_day_staged",
            "date": current.isoformat(),
            **quality,
        }

    async def _load_accrual_rows(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        start = date.fromisoformat(str(state["date_from"]))
        end = date.fromisoformat(str(state["date_to"]))
        rows: list[dict[str, Any]] = []
        day = start
        while day <= end:
            payload = await self._stage_read(
                str(state["job_id"]),
                "accrual-" + day.isoformat() + ".json",
            )
            if not isinstance(payload, list):
                raise RuntimeError(f"accrual staging for {day.isoformat()} is not a list")
            rows.extend(item for item in payload if isinstance(item, dict))
            day += timedelta(days=1)
        return rows

    async def _write_canonical_verified(
        self,
        parts: list[str],
        name: str,
        data: bytes,
    ) -> tuple[Any, str]:
        parent = await self.store.ensure_folder_path(parts)
        item = await self.store.upload_bytes(
            parent,
            name,
            data,
            mime_type="text/csv",
        )

        # Verify against the canonical Drive writer, not merely the local mirror.
        drive = getattr(self.store, "drive", None)
        if drive is not None:
            drive_parent = await drive.ensure_folder_path(parts)
            drive_item, drive_raw = await drive.download_named(drive_parent, name)
            if drive_item is None or drive_raw is None:
                raise RuntimeError(f"canonical Drive verification missing for {name}")
            if hashlib.sha256(drive_raw).hexdigest() != hashlib.sha256(data).hexdigest():
                raise RuntimeError(f"canonical Drive SHA256 verification failed for {name}")

        return item, hashlib.sha256(data).hexdigest()

    async def _publish(self, state: dict[str, Any]) -> dict[str, Any]:
        fbo = await self._stage_read(
            str(state["job_id"]),
            "ozon_current_orders_fbo.json",
        )
        fbs = await self._stage_read(
            str(state["job_id"]),
            "ozon_current_orders_fbs.json",
        )
        accruals = await self._load_accrual_rows(state)
        payloads = {
            "ozon_current_orders_fbo": fbo,
            "ozon_current_orders_fbs": fbs,
            "ozon_current_accruals": accruals,
        }

        published: dict[str, Any] = {}
        coverage_rows: list[dict[str, Any]] = []
        for dataset, rows in payloads.items():
            if not isinstance(rows, list):
                raise RuntimeError(f"{dataset} staging is not a list")
            cfg = DATASETS[dataset]
            stable = tuple(cfg["stable_key"])
            quality = stable_key_quality(rows, stable)
            if (
                quality["duplicate_stable_key_rows"]
                or quality["incomplete_stable_key_rows"]
            ):
                raise RuntimeError(f"{dataset} failed publish stable-key validation: {quality}")

            raw = serialize_snapshot(rows, stable_key=stable)
            parts, name = canonical_location(
                str(state["cabinet"]),
                int(state["year"]),
                int(state["month"]),
                dataset,
            )
            item, sha = await self._write_canonical_verified(parts, name, raw)
            published[dataset] = {
                "canonical_file": name,
                "file_id": str(getattr(item, "id", "") or ""),
                "rows": len(rows),
                "bytes": len(raw),
                "sha256": sha,
                **quality,
            }
            coverage_rows.append(
                {
                    "marketplace": "ozon",
                    "cabinet": state["cabinet"],
                    "dataset": dataset,
                    "period": state["period"],
                    "date_from": state["date_from"],
                    "date_to": state["date_to"],
                    "stable_key": "+".join(stable),
                    "rows": len(rows),
                    "canonical_file": name,
                    "canonical_file_id": str(getattr(item, "id", "") or ""),
                    "bytes": len(raw),
                    "sha256": sha,
                    "status": "COMPLETE",
                    "refreshed_at_utc": _utc_now(),
                }
            )

        coverage_bytes = serialize_coverage_registry(coverage_rows)
        coverage_parts, coverage_name = coverage_location(
            str(state["cabinet"]),
            int(state["year"]),
            int(state["month"]),
        )
        coverage_item, coverage_sha = await self._write_canonical_verified(
            coverage_parts,
            coverage_name,
            coverage_bytes,
        )

        state["datasets"] = published
        state["coverage_registry"] = {
            "canonical_file": coverage_name,
            "file_id": str(getattr(coverage_item, "id", "") or ""),
            "bytes": len(coverage_bytes),
            "sha256": coverage_sha,
        }
        state["phase"] = "COMPLETE"
        state["status"] = "COMPLETE"
        state["refresh_completed_at_utc"] = _utc_now()
        state["last_error"] = None
        state["last_retry_after_seconds"] = 0
        await self._save(state)
        await self._unschedule(str(state["job_id"]))
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": "COMPLETE",
            "phase": "COMPLETE",
            "action": "current_snapshot_published",
            "cabinet": state["cabinet"],
            "period": state["period"],
            "datasets": published,
            "coverage_registry": state["coverage_registry"],
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
                key=f"marketplace-archive:v2:ozon-current:job:{selected}",
                ttl_seconds=900,
            ):
                state = await self._load(selected)
                if state is None:
                    await self._unschedule(selected)
                    return {
                        "ok": False,
                        "error": "archive_job_not_found",
                        "job_id": selected,
                    }
                if state.get("status") == "COMPLETE":
                    await self._unschedule(selected)
                    return {
                        "ok": True,
                        "job_id": selected,
                        "status": "COMPLETE",
                        "action": "noop",
                    }
                state["status"] = "RUNNING"
                state["last_retry_after_seconds"] = 0
                state["last_error"] = None
                await self._save(state)

                phase = str(state.get("phase") or "")
                if phase == "FETCH_FBO":
                    return await self._fetch_posting_dataset(
                        state,
                        dataset="ozon_current_orders_fbo",
                    )
                if phase == "FETCH_FBS":
                    return await self._fetch_posting_dataset(
                        state,
                        dataset="ozon_current_orders_fbs",
                    )
                if phase == "FETCH_ACCRUALS":
                    return await self._fetch_accrual_day(state)
                if phase == "PUBLISH":
                    return await self._publish(state)

                state["status"] = "FAILED"
                state["last_error"] = f"Unknown Ozon CURRENT phase: {phase}"
                await self._save(state)
                await self._unschedule(selected)
                return {
                    "ok": False,
                    "job_id": selected,
                    "status": "FAILED",
                    "error": state["last_error"],
                }
        except RuntimeError as exc:
            if "already running" in str(exc):
                return {
                    "ok": True,
                    "job_id": selected,
                    "status": "BUSY",
                    "action": "worker_busy",
                    "retry_after_seconds": 2,
                }
            state = await self._load(selected)
            if state is not None:
                state["status"] = "FAILED"
                state["last_error"] = str(exc)[:2000]
                await self._save(state)
                await self._unschedule(selected)
            return {
                "ok": False,
                "job_id": selected,
                "status": "FAILED",
                "error": str(exc),
            }


def serialize_coverage_registry(rows: list[dict[str, Any]]) -> bytes:
    fields = [
        "marketplace",
        "cabinet",
        "dataset",
        "period",
        "date_from",
        "date_to",
        "stable_key",
        "rows",
        "canonical_file",
        "canonical_file_id",
        "bytes",
        "sha256",
        "status",
        "refreshed_at_utc",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        delimiter=";",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _cell(row.get(field)) for field in fields})
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


__all__ = [
    "DATASETS",
    "OZON_ARCHIVE_CABINETS",
    "OzonCurrentArchiveJobQueue",
    "canonical_location",
    "coverage_location",
    "current_period",
    "month_key",
    "parse_snapshot",
    "provider_utc_window",
    "serialize_coverage_registry",
    "serialize_snapshot",
    "stable_key_quality",
]
