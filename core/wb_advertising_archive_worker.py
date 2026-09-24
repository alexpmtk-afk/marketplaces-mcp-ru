"""Verified canonical commit worker for WB Advertising Archive V1.

Ingestion writes provider-normalized data only to durable Yandex job staging.
This worker merges one staged dataset with its canonical annual dataset, creates
an immutable Yandex candidate, uploads it to a non-canonical Drive staging file,
verifies exact size/SHA256, mirrors the exact bytes to Yandex canonical backup,
promotes the verified Drive file, and only then commits dataset coverage.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

from .archive_coverage import coverage_record, merge_coverage_registry
from .archive_drive_resumable import (
    GoogleDriveResumableUploader,
    ResumableUploadError,
    build_drive_resumable_uploader_from_bridge,
)
from .archive_google import ArchiveStorageError
from .wb_advertising_archive import (
    canonical_location,
    coverage_registry_location,
    encode_csv,
    merge_annual_dataset,
    parse_csv,
)
from .wb_advertising_archive_queue import JOB_FOLDER, WBAdvertisingArchiveJobQueue
from .wb_finance_archive import ArchiveLock

DATASET_FIELDS: dict[str, tuple[str, ...]] = {
    "ads_campaign_roster_snapshots": (
        "observed_at", "campaign_id", "campaign_type", "status", "change_time", "fullstats_eligible",
    ),
    "ads_campaign_daily": (
        "date", "campaign_id", "views", "clicks", "cart_adds", "ad_orders",
        "advertised_items", "canceled", "spend", "attributed_order_amount",
    ),
    "ads_product_daily": (
        "date", "campaign_id", "app_type", "nm_id", "product_role", "name", "views", "clicks",
        "cart_adds", "ad_orders", "advertised_items", "canceled", "spend",
        "attributed_order_amount", "avg_position",
    ),
    "ads_search_cluster_daily": (
        "date", "campaign_id", "nm_id", "norm_query", "payment_type", "views", "clicks",
        "cart_adds", "orders", "ordered_items", "spend", "avg_position", "ctr", "cpc",
        "cpm", "quality_flags",
    ),
    "ads_expenses": (
        "event_fingerprint", "upd_num", "upd_time", "upd_sum", "advert_id", "campaign_name",
        "advert_type", "payment_type", "advert_status", "request_date_from", "request_date_to",
    ),
    "ads_payments": (
        "event_key", "payment_id", "date", "sum", "type", "status_id", "card_status",
        "request_date_from", "request_date_to",
    ),
    "ads_campaign_snapshots": (
        "observed_at", "campaign_id", "status", "payment_type", "name", "type", "raw_json",
    ),
}


def _dataset_lock_key(cabinet: str, year: int, dataset: str) -> str:
    return f"marketplace-archive:v3:wb-ads:commit:{cabinet}:{dataset}:{int(year)}"


def _empty_csv(dataset: str) -> bytes:
    fields = DATASET_FIELDS.get(dataset)
    if not fields:
        raise ValueError(f"No canonical schema for advertising dataset {dataset}")
    return encode_csv(fields, [])


def _metadata_matches(meta: dict[str, Any], expected_bytes: int, expected_sha: str) -> bool:
    try:
        size = int(meta.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return size == int(expected_bytes) and str(meta.get("sha256Checksum") or "").lower() == str(expected_sha).lower()


class WBAdvertisingArchiveWorker:
    """Run ingestion and verified canonical commit through one durable worker."""

    def __init__(
        self,
        queue: WBAdvertisingArchiveJobQueue,
        store: Any,
        uploader: GoogleDriveResumableUploader | None = None,
    ) -> None:
        self.queue = queue
        self.store = store
        drive = getattr(store, "drive", None)
        self.uploader = (
            uploader
            if uploader is not None
            else (build_drive_resumable_uploader_from_bridge(drive) if drive is not None else None)
        )

    async def worker_step(self, job_id: str = "") -> dict[str, Any]:
        selected = str(job_id).strip()
        if not selected:
            selected, wait = await self.queue._next_due()
            if not selected:
                return {
                    "ok": True,
                    "action": "idle",
                    "retry_after_seconds": int(math.ceil(wait)) if wait > 0 else 0,
                }
        state = await self.queue._load(selected)
        if state is None:
            await self.queue._unschedule(selected)
            return {"ok": False, "error": "advertising_archive_job_not_found", "job_id": selected}
        if state.get("status") == "COMPLETE":
            await self.queue._unschedule(selected)
            return {"ok": True, "job_id": selected, "status": "COMPLETE", "action": "noop"}
        if state.get("status") not in {
            "READY_TO_COMMIT", "COMMITTING", "PROMOTION_PENDING", "WAITING_COMMIT_RETRY"
        } and str(state.get("phase") or "") != "COMMIT":
            return await self.queue.worker_step(selected)
        return await self._commit_step(state)

    async def _commit_step(self, state: dict[str, Any]) -> dict[str, Any]:
        job_id = str(state["job_id"])
        commit = dict(state.get("commit") or {})
        datasets = list(commit.get("datasets") or sorted((state.get("staged_datasets") or {}).keys()))
        if not datasets:
            state["status"] = "FAILED"
            state["last_error"] = "Advertising ingestion produced no staged datasets"
            await self.queue._save(state)
            await self.queue._unschedule(job_id)
            return {"ok": False, "job_id": job_id, "status": "FAILED", "error": state["last_error"]}
        index = int(commit.get("index", 0) or 0)
        if index >= len(datasets):
            state["phase"] = "COMPLETE"
            state["status"] = "COMPLETE"
            state["last_error"] = None
            state["commit"] = {**commit, "datasets": datasets, "index": len(datasets), "phase": "COMPLETE"}
            await self.queue._save(state)
            await self.queue._unschedule(job_id)
            return {
                "ok": True,
                "job_id": job_id,
                "status": "COMPLETE",
                "action": "advertising_archive_committed",
                "datasets": datasets,
            }

        dataset = str(datasets[index])
        cabinet = str(state["cabinet"])
        year = int(state["year"])
        try:
            async with ArchiveLock(key=_dataset_lock_key(cabinet, year, dataset), ttl_seconds=900):
                state = await self.queue._load(job_id) or state
                commit = dict(state.get("commit") or commit)
                datasets = list(commit.get("datasets") or datasets)
                index = int(commit.get("index", index) or 0)
                dataset = str(datasets[index])
                return await self._commit_dataset(state, commit, dataset)
        except RuntimeError as exc:
            if "already running" in str(exc):
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": "BUSY",
                    "action": "dataset_commit_busy",
                    "dataset": dataset,
                    "retry_after_seconds": 2,
                }
            raise

    async def _commit_dataset(
        self,
        state: dict[str, Any],
        commit: dict[str, Any],
        dataset: str,
    ) -> dict[str, Any]:
        phase = str(commit.get("dataset_phase") or "PREPARE")
        if phase == "PREPARE":
            return await self._prepare_dataset(state, commit, dataset)
        if phase in {"UPLOAD", "PROMOTION_PENDING"}:
            return await self._upload_dataset(state, commit, dataset)
        if phase == "COVERAGE":
            return await self._coverage_dataset(state, commit, dataset)
        raise RuntimeError(f"Unknown advertising commit dataset phase: {phase}")

    async def _prepare_dataset(
        self,
        state: dict[str, Any],
        commit: dict[str, Any],
        dataset: str,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        cabinet = str(state["cabinet"])
        year = int(state["year"])
        stage_parent, stage_name = await self.queue._stage_location(job_id, dataset)
        _, staged_raw = await self.store.download_named(stage_parent, stage_name)
        _, staged_rows = parse_csv(staged_raw)

        annual_parts, annual_name = canonical_location(cabinet, year, dataset)
        annual_parent = await self.store.ensure_folder_path(annual_parts)
        current_item, current_raw = await self.store.download_named(annual_parent, annual_name)
        candidate, stats = merge_annual_dataset(dataset, current_raw, staged_rows)
        if not candidate:
            candidate = _empty_csv(dataset)
            _, candidate_rows = parse_csv(candidate)
            stats = {
                "added_rows": 0,
                "total_rows": len(candidate_rows),
                "columns": len(DATASET_FIELDS[dataset]),
                "bytes": len(candidate),
                "sha256": hashlib.sha256(candidate).hexdigest(),
            }

        candidate_parent = await self.store.ensure_folder_path([*JOB_FOLDER, job_id, "commit", dataset])
        candidate_item = await self.store.upload_bytes(
            candidate_parent,
            "annual-candidate.csv",
            candidate,
            mime_type="text/csv",
        )
        expected_sha = hashlib.sha256(candidate).hexdigest()
        if getattr(candidate_item, "size", None) is not None and int(candidate_item.size) != len(candidate):
            raise RuntimeError("Advertising annual candidate failed Yandex size verification")

        commit.update({
            "datasets": list(commit.get("datasets") or sorted((state.get("staged_datasets") or {}).keys())),
            "index": int(commit.get("index", 0) or 0),
            "dataset": dataset,
            "dataset_phase": "UPLOAD",
            "candidate_id": str(candidate_item.id),
            "annual_parts": annual_parts,
            "annual_name": annual_name,
            "annual_bytes": len(candidate),
            "annual_sha256": expected_sha,
            "annual_rows": int(stats.get("total_rows", 0) or 0),
            "previous_canonical_file_id": str(getattr(current_item, "id", "") or "") or None,
            "upload": {},
        })
        state["commit"] = commit
        state["phase"] = "COMMIT"
        state["status"] = "COMMITTING"
        state["last_error"] = None
        await self.queue._save(state)
        await self.queue._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "status": "COMMITTING",
            "action": "advertising_candidate_prepared",
            "dataset": dataset,
            "rows": commit["annual_rows"],
            "bytes": commit["annual_bytes"],
        }

    async def _upload_dataset(
        self,
        state: dict[str, Any],
        commit: dict[str, Any],
        dataset: str,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        if self.uploader is None:
            state["status"] = "WAITING_CONFIGURATION"
            state["last_error"] = "Verified Drive resumable uploader is not configured"
            await self.queue._save(state)
            await self.queue._unschedule(job_id)
            return {
                "ok": True,
                "job_id": job_id,
                "status": "WAITING_CONFIGURATION",
                "action": "drive_resumable_required",
                "dataset": dataset,
            }

        expected_bytes = int(commit.get("annual_bytes", 0) or 0)
        expected_sha = str(commit.get("annual_sha256") or "").lower()
        candidate_id = str(commit.get("candidate_id") or "")
        annual_parts = [str(item) for item in commit.get("annual_parts") or []]
        annual_name = str(commit.get("annual_name") or "")
        if expected_bytes <= 0 or len(expected_sha) != 64 or not candidate_id or not annual_parts or not annual_name:
            raise RuntimeError("Advertising commit metadata is incomplete")

        drive = getattr(self.store, "drive", None)
        yandex = getattr(self.store, "yandex", self.store)
        if drive is None:
            raise RuntimeError("Advertising canonical commit requires HybridArchiveStore")
        drive_parent = await drive.ensure_folder_path(annual_parts)
        upload = dict(commit.get("upload") or {})
        staging_name = str(upload.get("staging_name") or f".{annual_name}.upload-{expected_sha[:12]}.tmp")
        upload["staging_name"] = staging_name

        try:
            # Recovery after a lost promotion response: exact canonical bytes are authoritative.
            canonical = await self.uploader.find_named_file(drive_parent, annual_name)
            canonical_id = str((canonical or {}).get("id") or "")
            if canonical_id:
                canonical_meta = await self.uploader.file_metadata(canonical_id)
                if _metadata_matches(canonical_meta, expected_bytes, expected_sha):
                    commit["canonical_file_id"] = canonical_id
                    commit["dataset_phase"] = "COVERAGE"
                    commit["upload"] = upload
                    state["commit"] = commit
                    state["status"] = "COMMITTING"
                    state["last_error"] = None
                    await self.queue._save(state)
                    await self.queue._schedule(job_id, 0)
                    return {
                        "ok": True,
                        "job_id": job_id,
                        "action": "canonical_already_verified",
                        "dataset": dataset,
                    }

            staged_id = str(upload.get("staged_file_id") or "")
            session_uri = str(upload.get("session_uri") or "")
            if staged_id and upload.get("promotion_attempted"):
                return await self._finish_upload(state, commit, dataset, staged_id)

            if session_uri:
                probe = await self.uploader.query_status(session_uri, expected_bytes)
                if probe.state == "complete":
                    file_id = str((probe.file or {}).get("id") or upload.get("target_file_id") or "")
                    return await self._finish_upload(state, commit, dataset, file_id)
                if probe.state == "expired":
                    upload = {"staging_name": staging_name}
                    commit["upload"] = upload
                    session_uri = ""
                else:
                    upload["offset"] = int(probe.offset)

            if not session_uri:
                staged = await self.uploader.find_named_file(drive_parent, staging_name)
                staged_id = str((staged or {}).get("id") or "")
                if staged_id:
                    staged_meta = await self.uploader.file_metadata(staged_id)
                    if _metadata_matches(staged_meta, expected_bytes, expected_sha):
                        upload["staged_file_id"] = staged_id
                        upload["offset"] = expected_bytes
                        commit["upload"] = upload
                        state["commit"] = commit
                        await self.queue._save(state)
                        return await self._finish_upload(state, commit, dataset, staged_id)
                session = await self.uploader.start_session(
                    parent_id=drive_parent,
                    name=staging_name,
                    total_bytes=expected_bytes,
                    mime_type="text/csv",
                )
                upload.update({
                    "session_uri": session.uri,
                    "target_file_id": session.file_id,
                    "offset": 0,
                    "retry_count": 0,
                })
                commit["upload"] = upload
                state["commit"] = commit
                state["status"] = "COMMITTING"
                await self.queue._save(state)
                await self.queue._schedule(job_id, 0)
                return {
                    "ok": True,
                    "job_id": job_id,
                    "action": "drive_resumable_session_started",
                    "dataset": dataset,
                    "bytes": expected_bytes,
                }

            offset = max(0, int(upload.get("offset", 0) or 0))
            if offset >= expected_bytes:
                probe = await self.uploader.query_status(session_uri, expected_bytes)
                if probe.state == "complete":
                    file_id = str((probe.file or {}).get("id") or upload.get("target_file_id") or "")
                    return await self._finish_upload(state, commit, dataset, file_id)
                upload["offset"] = int(probe.offset)
                commit["upload"] = upload
                state["commit"] = commit
                await self.queue._save(state)
                await self.queue._schedule(job_id, 0)
                return {
                    "ok": True,
                    "job_id": job_id,
                    "action": "drive_offset_reconciled",
                    "dataset": dataset,
                    "confirmed_bytes": int(probe.offset),
                }

            end = min(expected_bytes, offset + self.uploader.chunk_size)
            chunk = await self._read_yandex_range(yandex, candidate_id, offset, end)
            if len(chunk) != end - offset:
                raise RuntimeError("Yandex advertising candidate returned an unexpected byte range")
            progress = await self.uploader.upload_chunk(
                session_uri=session_uri,
                offset=offset,
                total_bytes=expected_bytes,
                data=chunk,
            )
            if progress.state == "expired":
                commit["upload"] = {"staging_name": staging_name}
                state["commit"] = commit
                await self.queue._save(state)
                await self.queue._schedule(job_id, 0)
                return {"ok": True, "job_id": job_id, "action": "drive_session_restarted", "dataset": dataset}
            if progress.state == "complete":
                file_id = str((progress.file or {}).get("id") or upload.get("target_file_id") or "")
                return await self._finish_upload(state, commit, dataset, file_id)
            upload["offset"] = int(progress.offset)
            upload["retry_count"] = 0
            commit["upload"] = upload
            state["commit"] = commit
            state["status"] = "COMMITTING"
            state["last_error"] = None
            await self.queue._save(state)
            await self.queue._schedule(job_id, 0)
            return {
                "ok": True,
                "job_id": job_id,
                "action": "drive_chunk_uploaded",
                "dataset": dataset,
                "confirmed_bytes": int(progress.offset),
                "total_bytes": expected_bytes,
                "progress_percent": round(100.0 * int(progress.offset) / expected_bytes, 2),
            }
        except (ResumableUploadError, ArchiveStorageError) as exc:
            return await self._retry_or_fail(state, commit, dataset, exc)

    async def _finish_upload(
        self,
        state: dict[str, Any],
        commit: dict[str, Any],
        dataset: str,
        file_id: str,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        expected_bytes = int(commit["annual_bytes"])
        expected_sha = str(commit["annual_sha256"]).lower()
        candidate_id = str(commit["candidate_id"])
        annual_parts = [str(item) for item in commit["annual_parts"]]
        annual_name = str(commit["annual_name"])
        upload = dict(commit.get("upload") or {})
        staging_name = str(upload.get("staging_name") or "")
        if not file_id:
            drive_parent = await self.store.drive.ensure_folder_path(annual_parts)
            found = await self.uploader.find_named_file(drive_parent, staging_name)
            file_id = str((found or {}).get("id") or "")
        if not file_id:
            raise RuntimeError("Drive upload completed without advertising staged file id")
        meta = await self.uploader.file_metadata(file_id)
        if not _metadata_matches(meta, expected_bytes, expected_sha):
            raise RuntimeError("Advertising staged Drive file failed exact size/SHA256 verification")

        yandex = getattr(self.store, "yandex", self.store)
        candidate_raw = await yandex.download_bytes(candidate_id)
        if len(candidate_raw) != expected_bytes or hashlib.sha256(candidate_raw).hexdigest() != expected_sha:
            raise RuntimeError("Advertising candidate changed before canonical backup")
        backup_parent = await yandex.ensure_folder_path(annual_parts)
        backup = await yandex.upload_bytes(backup_parent, annual_name, candidate_raw, mime_type="text/csv")
        if getattr(backup, "size", None) is not None and int(backup.size) != expected_bytes:
            raise RuntimeError("Advertising Yandex backup failed size verification")

        upload["staged_file_id"] = file_id
        upload["promotion_attempted"] = True
        upload["offset"] = expected_bytes
        commit["upload"] = upload
        commit["dataset_phase"] = "PROMOTION_PENDING"
        state["commit"] = commit
        state["status"] = "PROMOTION_PENDING"
        state["last_error"] = None
        await self.queue._save(state)

        drive_parent = await self.store.drive.ensure_folder_path(annual_parts)
        previous_id = str(commit.get("previous_canonical_file_id") or "") or None
        promoted = await self.store.drive.promote_verified_file(
            parent_id=drive_parent,
            file_id=file_id,
            staging_name=staging_name,
            canonical_name=annual_name,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha,
            previous_file_id=previous_id,
        )
        commit["canonical_file_id"] = str(promoted.id)
        commit["dataset_phase"] = "COVERAGE"
        commit.pop("upload", None)
        state["commit"] = commit
        state["status"] = "COMMITTING"
        state["last_error"] = None
        await self.queue._save(state)
        await self.queue._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "action": "advertising_canonical_promoted",
            "dataset": dataset,
            "drive_verified": True,
            "backup_verified": True,
        }

    async def _coverage_dataset(
        self,
        state: dict[str, Any],
        commit: dict[str, Any],
        dataset: str,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        cabinet = str(state["cabinet"])
        annual_name = str(commit["annual_name"])
        expected_bytes = int(commit["annual_bytes"])
        expected_sha = str(commit["annual_sha256"])
        rows = int(commit.get("annual_rows", 0) or 0)
        matching = [
            request
            for request in state.get("completed_requests") or []
            if dataset in (request.get("datasets") or [])
        ]
        if not matching:
            raise RuntimeError(f"No completed provider requests prove advertising dataset {dataset}")

        quality_flags: list[str] = []
        if dataset in {"ads_campaign_roster_snapshots", "ads_campaign_snapshots"}:
            quality_flags.append("observation_snapshot_not_event_history")
        if dataset == "ads_search_cluster_daily":
            stage_parent, stage_name = await self.queue._stage_location(job_id, dataset)
            _, raw = await self.store.download_named(stage_parent, stage_name)
            if raw and b"cpc_views_ctr_cpm_not_available" in raw:
                quality_flags.append("cpc_views_ctr_cpm_not_available")
        quality_status = "PASS_WITH_FLAGS" if quality_flags else "PASS"

        records = [
            coverage_record(
                marketplace="wb",
                cabinet=cabinet,
                dataset=dataset,
                operation_id=str(request["operation_id"]),
                date_from=str(request.get("date_from") or f"{state['year']}-01-01"),
                date_to=str(request.get("date_to") or f"{state['year']}-12-31"),
                scope=request.get("scope") or {},
                annual_file=annual_name,
                rows=rows,
                bytes_count=expected_bytes,
                sha256=expected_sha,
                quality_status=quality_status,
                quality_flags=quality_flags,
            )
            for request in matching
        ]
        registry_parts, registry_name = coverage_registry_location()
        registry_parent = await self.store.ensure_folder_path(registry_parts)
        _, registry_raw = await self.store.download_named(registry_parent, registry_name)
        merged = merge_coverage_registry(registry_raw, records)
        await self.store.upload_bytes(
            registry_parent,
            registry_name,
            merged,
            mime_type="text/csv",
        )

        index = int(commit.get("index", 0) or 0) + 1
        datasets = list(commit.get("datasets") or [])
        commit = {
            "datasets": datasets,
            "index": index,
            "dataset_phase": "PREPARE",
            "committed_datasets": [*(commit.get("committed_datasets") or []), dataset],
        }
        state["commit"] = commit
        state["status"] = "COMMITTING" if index < len(datasets) else "READY_TO_FINALIZE"
        state["last_error"] = None
        await self.queue._save(state)
        await self.queue._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "action": "advertising_coverage_committed",
            "dataset": dataset,
            "coverage_records": len(records),
            "dataset_index": index,
            "dataset_total": len(datasets),
        }

    async def _retry_or_fail(
        self,
        state: dict[str, Any],
        commit: dict[str, Any],
        dataset: str,
        exc: Exception,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        retryable = bool(getattr(exc, "retryable", False))
        if not retryable:
            state["status"] = "FAILED"
            state["last_error"] = str(exc)[:1000]
            state["commit"] = commit
            await self.queue._save(state)
            await self.queue._unschedule(job_id)
            return {
                "ok": False,
                "job_id": job_id,
                "status": "FAILED",
                "dataset": dataset,
                "error": state["last_error"],
            }
        upload = dict(commit.get("upload") or {})
        retry_count = int(upload.get("retry_count", 0) or 0) + 1
        upload["retry_count"] = retry_count
        commit["upload"] = upload
        delay = max(self._retry_delay(job_id, dataset, retry_count), int(math.ceil(float(getattr(exc, "retry_after_seconds", 0.0) or 0.0))))
        state["status"] = "WAITING_COMMIT_RETRY"
        state["last_error"] = str(exc)[:1000]
        state["commit"] = commit
        await self.queue._save(state)
        await self.queue._schedule(job_id, delay)
        return {
            "ok": True,
            "job_id": job_id,
            "status": "WAITING_COMMIT_RETRY",
            "action": "advertising_commit_waiting_retry",
            "dataset": dataset,
            "retry_after_seconds": delay,
        }

    @staticmethod
    def _retry_delay(job_id: str, dataset: str, retry_count: int) -> int:
        exponent = min(max(int(retry_count) - 1, 0), 6)
        base = min(300, 5 * (2 ** exponent))
        jitter = hashlib.sha256(f"{job_id}:{dataset}:{retry_count}".encode("utf-8")).digest()[0] % 6
        return min(300, base + jitter)

    @staticmethod
    async def _read_yandex_range(yandex: Any, file_id: str, start: int, end: int) -> bytes:
        if hasattr(yandex, "download_range"):
            return await yandex.download_range(file_id, start, end)
        response = await yandex._request(
            "GET",
            yandex._object_url(file_id),
            headers={"Range": f"bytes={int(start)}-{int(end) - 1}"},
        )
        return response.content
