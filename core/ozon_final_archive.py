"""Durable closed-month Ozon FINAL realization archive.

FINAL uses the provider's row-level realization posting report for every closed
calendar month. Each month is replaced as a whole on refresh, then the verified
monthly sources are rebuilt into one annual canonical CSV.

Google Drive is canonical. Queue/staging state and an exact mirror are kept on
the configured REMOTE durable backend.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .archive_drive_resumable import build_drive_resumable_uploader_from_bridge
from .business_registry import resolve_business_cabinet
from .ozon_current_archive import (
    MAX_ITEMS,
    OZON_ARCHIVE_CABINETS,
    parse_snapshot,
    serialize_snapshot,
    stable_key_quality,
)
from .paginate import fetch_all as fetch_all_pages
from .rate_limit import redis_connection_kwargs, redis_url_from_env
from .tools import (
    has_proven_quota,
    has_proven_read_semantics,
    resolve_named_cabinet,
)
from .wb_finance_archive import ArchiveLock

MOSCOW = ZoneInfo("Europe/Moscow")
OPERATION_ID = "ozon_post_v1_finance_realization_posting"
DATASET = "ozon_final_realization"
STABLE_KEY = ("report_year", "report_month", "row_number")
QUEUE_KEY = "marketplace-archive:v2:ozon-final:due"
JOB_FOLDER = ("app", "jobs", "ozon-final")
RESUMABLE_THRESHOLD = 3 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def closed_months(year: int, today: date | None = None) -> tuple[int, ...]:
    current = today or datetime.now(MOSCOW).date()
    value = int(year)
    if value > current.year:
        raise ValueError("Ozon FINAL year cannot be in the future")
    if value < current.year:
        return tuple(range(1, 13))
    return tuple(range(1, current.month))


def month_period(year: int, month: int) -> str:
    return f"{int(year):04d}-{int(month):02d}"


def monthly_location(cabinet: str, year: int, month: int) -> tuple[list[str], str]:
    period = month_period(year, month)
    return (
        ["База данных", "Ozon", cabinet, str(int(year)), "FINAL", "monthly_source"],
        f"{cabinet}__realization__{period}.csv",
    )


def annual_location(cabinet: str, year: int) -> tuple[list[str], str]:
    return (
        ["База данных", "Ozon", cabinet, str(int(year)), "FINAL", "annual"],
        f"{cabinet}__realization__{int(year)}.csv",
    )


def coverage_location(cabinet: str, year: int) -> tuple[list[str], str]:
    return (
        ["База данных", "Ozon", cabinet, str(int(year)), "FINAL"],
        "final_coverage_registry.csv",
    )


def enrich_realization_row(row: dict[str, Any], *, year: int, month: int) -> dict[str, Any]:
    value = dict(row)
    raw_row_number = value.get("row_number", value.get("rowNumber"))
    order = value.get("order") if isinstance(value.get("order"), dict) else {}
    item = value.get("item") if isinstance(value.get("item"), dict) else {}
    value["report_year"] = int(year)
    value["report_month"] = month_period(year, month)
    value["row_number"] = raw_row_number
    value["posting_number"] = order.get("posting_number")
    value["order_created_date"] = order.get("created_date")
    value["sku"] = item.get("sku")
    value["offer_id"] = item.get("offer_id")
    return value


def serialize_final_coverage(rows: list[dict[str, Any]]) -> bytes:
    fields = [
        "marketplace",
        "cabinet",
        "dataset",
        "period",
        "report_year",
        "report_month",
        "stable_key",
        "rows",
        "monthly_file",
        "monthly_file_id",
        "bytes",
        "sha256",
        "status",
        "refreshed_at_utc",
    ]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, delimiter=";", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else str(row.get(key)) for key in fields})
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def parse_final_coverage(raw: bytes | None) -> list[dict[str, str]]:
    if not raw:
        return []
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";")
    return [dict(row) for row in reader]


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


class OzonFinalArchiveJobQueue:
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
    def job_id(cabinet: str, year: int) -> str:
        return f"ozon-final-{cabinet}-{int(year)}"

    def _resolve_creds(self, cabinet: str) -> dict[str, str]:
        creds, error = resolve_named_cabinet(self.ozon.client, cabinet)
        if error:
            raise RuntimeError(str(error.get("message") or error))
        assert creds is not None
        return creds

    def _spec(self):
        spec = self.ozon.catalog.get(OPERATION_ID)
        if spec is None:
            raise RuntimeError(f"{OPERATION_ID} contract is missing")
        if not has_proven_read_semantics(spec):
            raise RuntimeError(f"{OPERATION_ID} read semantics are not proven")
        if not has_proven_quota(spec):
            raise RuntimeError(f"{OPERATION_ID} quota contract is not proven")
        return spec

    async def _redis(self):
        url = redis_url_from_env()
        if not url:
            raise RuntimeError("Shared Redis is required for Ozon FINAL scheduling")
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
                {str(job_id): time.time() + max(0.0, float(delay_seconds))},
            )
        finally:
            await client.aclose()

    async def _unschedule(self, job_id: str) -> None:
        client = await self._redis()
        try:
            await client.zrem(QUEUE_KEY, str(job_id))
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
        parent = await self.store.ensure_folder_path(JOB_FOLDER)
        return parent, f"{job_id}.json"

    async def _load(self, job_id: str) -> dict[str, Any] | None:
        parent, name = await self._job_location(job_id)
        item, raw = await self.store.download_named(parent, name)
        if item is None or raw is None:
            return None
        value = json.loads(raw.decode("utf-8"))
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

    async def enqueue(self, *, year: int, seller: str) -> dict[str, Any]:
        months = closed_months(int(year))
        if not months:
            raise ValueError("Ozon FINAL has no closed months for the requested year")
        cabinet = self.normalize_cabinet(seller)
        job_id = self.job_id(cabinet, int(year))
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
                "year": int(year),
                "month_cursor": existing.get("month_cursor"),
                "refresh_generation": int(existing.get("refresh_generation", 1) or 1),
            }
        if existing is not None:
            generation = int(existing.get("refresh_generation", 1) or 1) + 1
            action = "reopened_complete"

        state = {
            "schema_version": 1,
            "job_id": job_id,
            "marketplace": "ozon",
            "dataset_family": "ozon_final",
            "cabinet": cabinet,
            "year": int(year),
            "closed_months": list(months),
            "month_cursor": 0,
            "status": "QUEUED",
            "phase": "FETCH_MONTH",
            "provider_calls": 0,
            "refresh_generation": generation,
            "months": {},
            "annual": {},
            "coverage_registry": {},
            "created_at_utc": (
                existing.get("created_at_utc") if existing else _utc_now()
            ),
            "refresh_started_at_utc": _utc_now(),
            "last_error": None,
            "last_retry_after_seconds": 0,
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
            "phase": "FETCH_MONTH",
            "cabinet": cabinet,
            "year": int(year),
            "closed_months": list(months),
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
            "closed_months": state.get("closed_months") or [],
            "month_cursor": int(state.get("month_cursor", 0) or 0),
            "provider_calls": int(state.get("provider_calls", 0) or 0),
            "months": state.get("months") or {},
            "annual": state.get("annual") or {},
            "coverage_registry": state.get("coverage_registry") or {},
            "refresh_generation": int(state.get("refresh_generation", 1) or 1),
            "last_retry_after_seconds": state.get("last_retry_after_seconds", 0),
            "last_error": state.get("last_error"),
            "created_at_utc": state.get("created_at_utc"),
            "updated_at_utc": state.get("updated_at_utc"),
        }

    async def _write_verified(
        self,
        parts: list[str],
        name: str,
        data: bytes,
    ) -> tuple[Any, str]:
        expected_sha = hashlib.sha256(data).hexdigest()
        if len(data) <= RESUMABLE_THRESHOLD or not hasattr(self.store, "drive"):
            parent = await self.store.ensure_folder_path(parts)
            item = await self.store.upload_bytes(parent, name, data, mime_type="text/csv")
            verifier = getattr(self.store, "reader", None) or getattr(self.store, "drive", None)
            if verifier is not None:
                verify_parent = await verifier.ensure_folder_path(parts)
                verify_item, verify_raw = await verifier.download_named(verify_parent, name)
                if verify_item is None or verify_raw is None:
                    raise RuntimeError(f"canonical Drive verification missing for {name}")
                if hashlib.sha256(verify_raw).hexdigest() != expected_sha:
                    raise RuntimeError(f"canonical Drive SHA256 verification failed for {name}")
            return item, expected_sha

        drive = self.store.drive
        durable = getattr(self.store, "yandex", None) or getattr(self.store, "durable", None)
        if durable is None:
            raise RuntimeError("Large FINAL publication requires REMOTE durable backup")

        drive_parent = await drive.ensure_folder_path(parts)
        local_parent = await durable.ensure_folder_path(parts)
        existing = await drive.find_child(drive_parent, name)
        previous_id = str(getattr(existing, "id", "") or "") or None
        if existing is not None:
            meta = await drive.file_metadata(str(existing.id))
            try:
                size = int(meta.get("size"))
            except (TypeError, ValueError):
                size = -1
            sha = str(meta.get("sha256Checksum") or "").lower()
            if size == len(data) and sha == expected_sha:
                backup = await durable.upload_bytes(local_parent, name, data, mime_type="text/csv")
                if getattr(backup, "size", None) is not None and int(backup.size) != len(data):
                    raise RuntimeError("REMOTE backup size mismatch")
                return existing, expected_sha

        uploader = build_drive_resumable_uploader_from_bridge(drive)
        staging_name = f".{name}.{expected_sha[:12]}.tmp"
        staged = await uploader.find_named_file(drive_parent, staging_name)
        staged_id = str((staged or {}).get("id") or "") or None

        if staged_id:
            meta = await uploader.file_metadata(staged_id)
            try:
                staged_size = int(meta.get("size") or 0)
            except (TypeError, ValueError):
                staged_size = 0
            staged_sha = str(meta.get("sha256Checksum") or "").lower()
            if staged_size != len(data) or staged_sha != expected_sha:
                await drive.trash_file(staged_id)
                staged_id = None

        if not staged_id:
            session = await uploader.start_session(
                parent_id=drive_parent,
                name=staging_name,
                total_bytes=len(data),
                mime_type="text/csv",
            )
            offset = 0
            while offset < len(data):
                end = min(len(data), offset + uploader.chunk_size)
                progress = await uploader.upload_chunk(
                    session_uri=session.uri,
                    offset=offset,
                    total_bytes=len(data),
                    data=data[offset:end],
                )
                if progress.state == "expired":
                    raise RuntimeError("Google Drive resumable session expired")
                offset = int(progress.offset)
                if progress.state == "complete":
                    staged_id = str((progress.file or {}).get("id") or session.file_id or "")
                    break
            if not staged_id:
                probe = await uploader.query_status(session.uri, len(data))
                if probe.state != "complete":
                    raise RuntimeError("Google Drive resumable upload did not complete")
                staged_id = str((probe.file or {}).get("id") or session.file_id or "")

        if not staged_id:
            raise RuntimeError("Google Drive resumable upload returned no file id")
        meta = await uploader.file_metadata(staged_id)
        try:
            actual_size = int(meta.get("size") or 0)
        except (TypeError, ValueError):
            actual_size = 0
        actual_sha = str(meta.get("sha256Checksum") or "").lower()
        if actual_size != len(data) or actual_sha != expected_sha:
            raise RuntimeError(f"Google Drive resumable verification failed for {name}")

        promoted = await drive.promote_verified_file(
            parent_id=drive_parent,
            file_id=staged_id,
            staging_name=staging_name,
            canonical_name=name,
            expected_bytes=len(data),
            expected_sha256=expected_sha,
            previous_file_id=previous_id,
        )
        backup = await durable.upload_bytes(local_parent, name, data, mime_type="text/csv")
        if getattr(backup, "size", None) is not None and int(backup.size) != len(data):
            raise RuntimeError("REMOTE backup size mismatch")
        backup_sha = str(getattr(backup, "sha256_checksum", "") or "").lower()
        if backup_sha and backup_sha != expected_sha:
            raise RuntimeError("REMOTE backup SHA256 mismatch")
        return promoted, expected_sha

    async def _fetch_month(self, state: dict[str, Any]) -> dict[str, Any]:
        months = [int(value) for value in state.get("closed_months") or []]
        cursor = int(state.get("month_cursor", 0) or 0)
        if cursor >= len(months):
            state["phase"] = "REBUILD_ANNUAL"
            state["status"] = "QUEUED"
            await self._save(state)
            await self._schedule(str(state["job_id"]), 0)
            return {
                "ok": True,
                "job_id": state["job_id"],
                "status": "QUEUED",
                "phase": "REBUILD_ANNUAL",
                "action": "closed_months_fetched",
            }

        month = months[cursor]
        spec = self._spec()
        creds = self._resolve_creds(str(state["cabinet"]))
        payload = await fetch_all_pages(
            self.ozon.client,
            spec,
            base_body={"month": int(month), "year": int(state["year"])},
            max_items=MAX_ITEMS,
            creds_override=creds,
        )
        if payload.get("ok") is not True:
            if _is_rate_limited(payload):
                retry = max(1.0, _retry_after(payload) or 1.0)
                state["status"] = "WAITING_RATE_LIMIT"
                state["last_retry_after_seconds"] = retry
                await self._save(state)
                await self._schedule(str(state["job_id"]), retry)
                return {
                    "ok": True,
                    "job_id": state["job_id"],
                    "status": "WAITING_RATE_LIMIT",
                    "phase": "FETCH_MONTH",
                    "month": month,
                    "retry_after_seconds": retry,
                }
            raise RuntimeError(
                "Ozon FINAL month fetch failed: "
                + json.dumps(payload, ensure_ascii=False, default=str)[:2000]
            )
        if bool(payload.get("truncated")):
            raise RuntimeError(f"Ozon FINAL {state['year']}-{month:02d} exceeded MAX_ITEMS")

        rows = payload.get("items") or []
        if not isinstance(rows, list):
            raise RuntimeError("Ozon FINAL realization returned non-list items")
        enriched = [
            enrich_realization_row(row, year=int(state["year"]), month=month)
            for row in rows
            if isinstance(row, dict)
        ]
        quality = stable_key_quality(enriched, STABLE_KEY)
        if quality["duplicate_stable_key_rows"] or quality["incomplete_stable_key_rows"]:
            raise RuntimeError(
                f"Ozon FINAL {state['year']}-{month:02d} failed stable-key validation: {quality}"
            )

        raw = serialize_snapshot(enriched, stable_key=STABLE_KEY)
        parts, name = monthly_location(
            str(state["cabinet"]),
            int(state["year"]),
            month,
        )
        item, sha = await self._write_verified(parts, name, raw)
        state["provider_calls"] = int(state.get("provider_calls", 0) or 0) + int(
            payload.get("pages_fetched", 1) or 1
        )
        state["months"][month_period(int(state["year"]), month)] = {
            "period": month_period(int(state["year"]), month),
            "rows": len(enriched),
            "bytes": len(raw),
            "sha256": sha,
            "monthly_file": name,
            "monthly_file_id": str(getattr(item, "id", "") or ""),
            **quality,
        }
        state["month_cursor"] = cursor + 1
        state["phase"] = (
            "FETCH_MONTH" if cursor + 1 < len(months) else "REBUILD_ANNUAL"
        )
        state["status"] = "QUEUED"
        state["last_error"] = None
        state["last_retry_after_seconds"] = 0
        await self._save(state)
        await self._schedule(str(state["job_id"]), 0)
        return {
            "ok": True,
            "job_id": state["job_id"],
            "status": "QUEUED",
            "phase": state["phase"],
            "action": "final_month_published",
            "period": month_period(int(state["year"]), month),
            "canonical_file": name,
            **quality,
        }

    async def _rebuild_annual(self, state: dict[str, Any]) -> dict[str, Any]:
        all_rows: list[dict[str, Any]] = []
        coverage_rows: list[dict[str, Any]] = []
        for month in [int(value) for value in state.get("closed_months") or []]:
            period = month_period(int(state["year"]), month)
            expected = dict((state.get("months") or {}).get(period) or {})
            parts, name = monthly_location(str(state["cabinet"]), int(state["year"]), month)
            parent = await self.store.ensure_folder_path(parts)
            item, raw = await self.store.download_named(parent, name)
            if item is None or raw is None:
                raise RuntimeError(f"Ozon FINAL monthly source missing: {name}")
            actual_sha = hashlib.sha256(raw).hexdigest()
            if str(expected.get("sha256") or "").lower() != actual_sha:
                raise RuntimeError(f"Ozon FINAL monthly source SHA mismatch: {name}")
            _fields, parsed = parse_snapshot(raw)
            month_rows: list[dict[str, Any]] = []
            for row in parsed:
                raw_json = str(row.get("_raw_json") or "")
                value = json.loads(raw_json) if raw_json else {}
                if not isinstance(value, dict):
                    raise RuntimeError(f"Ozon FINAL invalid raw JSON in {name}")
                month_rows.append(value)
            quality = stable_key_quality(month_rows, STABLE_KEY)
            if (
                quality["duplicate_stable_key_rows"]
                or quality["incomplete_stable_key_rows"]
                or len(month_rows) != int(expected.get("rows", -1))
            ):
                raise RuntimeError(f"Ozon FINAL monthly source quality mismatch: {name}")
            all_rows.extend(month_rows)
            coverage_rows.append({
                "marketplace": "ozon",
                "cabinet": state["cabinet"],
                "dataset": DATASET,
                "period": period,
                "report_year": state["year"],
                "report_month": period,
                "stable_key": "+".join(STABLE_KEY),
                "rows": len(month_rows),
                "monthly_file": name,
                "monthly_file_id": str(getattr(item, "id", "") or expected.get("monthly_file_id") or ""),
                "bytes": len(raw),
                "sha256": actual_sha,
                "status": "COMPLETE",
                "refreshed_at_utc": _utc_now(),
            })

        annual_quality = stable_key_quality(all_rows, STABLE_KEY)
        if (
            annual_quality["duplicate_stable_key_rows"]
            or annual_quality["incomplete_stable_key_rows"]
        ):
            raise RuntimeError(f"Ozon FINAL annual stable-key validation failed: {annual_quality}")

        annual_raw = serialize_snapshot(all_rows, stable_key=STABLE_KEY)
        annual_parts, annual_name = annual_location(
            str(state["cabinet"]), int(state["year"])
        )
        annual_item, annual_sha = await self._write_verified(
            annual_parts, annual_name, annual_raw
        )
        state["annual"] = {
            "canonical_file": annual_name,
            "file_id": str(getattr(annual_item, "id", "") or ""),
            "rows": len(all_rows),
            "bytes": len(annual_raw),
            "sha256": annual_sha,
            **annual_quality,
        }

        coverage_raw = serialize_final_coverage(coverage_rows)
        coverage_parts, coverage_name = coverage_location(
            str(state["cabinet"]), int(state["year"])
        )
        coverage_item, coverage_sha = await self._write_verified(
            coverage_parts, coverage_name, coverage_raw
        )
        state["coverage_registry"] = {
            "canonical_file": coverage_name,
            "file_id": str(getattr(coverage_item, "id", "") or ""),
            "rows": len(coverage_rows),
            "bytes": len(coverage_raw),
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
            "action": "final_annual_published",
            "cabinet": state["cabinet"],
            "year": state["year"],
            "closed_months": state["closed_months"],
            "annual": state["annual"],
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
                key=f"marketplace-archive:v2:ozon-final:job:{selected}",
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
                if phase == "FETCH_MONTH":
                    return await self._fetch_month(state)
                if phase == "REBUILD_ANNUAL":
                    return await self._rebuild_annual(state)
                raise RuntimeError(f"Unknown Ozon FINAL phase: {phase}")
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


__all__ = [
    "DATASET",
    "OPERATION_ID",
    "OzonFinalArchiveJobQueue",
    "STABLE_KEY",
    "annual_location",
    "closed_months",
    "coverage_location",
    "enrich_realization_row",
    "month_period",
    "monthly_location",
    "parse_final_coverage",
    "serialize_final_coverage",
]
