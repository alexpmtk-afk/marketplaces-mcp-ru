"""Durable large-file upload step for the marketplace archive queue.

The ordinary queue keeps discovery, WB download, PREPARE and COMMIT unchanged.
Only ``UPLOAD_ANNUAL`` is intercepted here. Large annual data is uploaded to a
non-canonical Drive staging filename first. After exact Drive SHA256 validation
and a byte-for-byte Yandex backup, Apps Script promotes that verified file to
the canonical name and trashes the previous canonical file.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

from .archive_drive_resumable import (
    GoogleDriveResumableUploader,
    ResumableUploadError,
    build_drive_resumable_uploader_from_bridge,
)
from .archive_google import ArchiveStorageError
from .archive_queue import JOB_FOLDER, WBFinanceArchiveJobQueue
from .wb_finance_archive import ArchiveLock


class WBFinanceResumableWorker:
    """Wrap the WB archive queue and replace only its large annual upload."""

    def __init__(
        self,
        queue: WBFinanceArchiveJobQueue,
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
            return {"ok": False, "error": "archive_job_not_found", "job_id": selected}
        finalize = state.get("finalize") or {}
        if str(finalize.get("phase") or "") != "UPLOAD_ANNUAL":
            return await self.queue.worker_step(selected)

        try:
            async with ArchiveLock(
                key=f"marketplace-archive:v2:wb-finance:job:{selected}",
                ttl_seconds=900,
            ):
                state = await self.queue._load(selected)
                if state is None:
                    await self.queue._unschedule(selected)
                    return {"ok": False, "error": "archive_job_not_found", "job_id": selected}
                if state.get("status") == "COMPLETE":
                    await self.queue._unschedule(selected)
                    return {"ok": True, "job_id": selected, "status": "COMPLETE", "action": "noop"}
                finalize = dict(state.get("finalize") or {})
                if str(finalize.get("phase") or "") != "UPLOAD_ANNUAL":
                    state["status"] = "QUEUED"
                    await self.queue._save(state)
                    await self.queue._schedule(selected, 0)
                    return {
                        "ok": True,
                        "job_id": selected,
                        "status": "QUEUED",
                        "action": "upload_phase_already_advanced",
                    }
                return await self._upload_step(state, finalize)
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

    async def _upload_step(
        self,
        state: dict[str, Any],
        finalize: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        report_id = int(finalize.get("report_id") or 0)
        expected_bytes = int(finalize.get("annual_bytes", 0) or 0)
        expected_sha = str(finalize.get("annual_sha256") or "").lower()
        if expected_bytes <= 0 or len(expected_sha) != 64:
            raise RuntimeError("Durable annual finalize metadata is incomplete")

        yandex = getattr(self.store, "yandex", self.store)
        drive = getattr(self.store, "drive", None)
        if drive is None:
            raise RuntimeError("Resumable annual upload requires the hybrid Drive/Yandex store")

        candidate_parent = await yandex.ensure_folder_path([*JOB_FOLDER, job_id, "finalize"])
        candidate_obj = await yandex.find_child(candidate_parent, "annual-candidate.csv")
        if candidate_obj is None:
            raise RuntimeError("Durable annual finalize candidate is missing")
        if candidate_obj.size is not None and int(candidate_obj.size) != expected_bytes:
            raise RuntimeError("Durable annual finalize candidate failed size verification")

        if self.uploader is None:
            state["finalize"] = finalize
            state["status"] = "WAITING_CONFIGURATION"
            state["last_error"] = (
                "Google Drive resumable Apps Script session broker is not configured; candidate is preserved"
            )
            await self.queue._save(state)
            await self.queue._unschedule(job_id)
            return {
                "ok": True,
                "job_id": job_id,
                "status": state["status"],
                "phase": state.get("phase"),
                "action": "drive_resumable_bridge_required",
                "report_id": report_id,
                "annual_bytes": expected_bytes,
                "candidate_preserved": True,
            }

        cabinet = str(state["cabinet"])
        year = int(state["year"])
        annual_parts = [
            "База данных", "WB", cabinet, str(year), "finance", "weekly", "main"
        ]
        drive_parent = await drive.ensure_folder_path(annual_parts)
        annual_name = f"{cabinet}__weekly_main__{year}.csv"
        staging_name = f".{annual_name}.upload-report-{report_id}-{expected_sha[:12]}.tmp"
        upload = dict(finalize.get("resumable_upload") or {})
        session_uri = str(upload.get("session_uri") or "")

        try:
            # Once promotion has been attempted, recover only by explicit IDs.
            # This avoids ambiguous name lookups during the tiny interval where
            # both old and new files may carry the canonical name.
            staged_checkpoint_id = str(upload.get("staged_file_id") or "")
            if upload.get("promotion_attempted") and staged_checkpoint_id:
                return await self._finish_staged_upload(
                    state=state,
                    finalize=finalize,
                    file_meta={"id": staged_checkpoint_id},
                    candidate_id=candidate_obj.id,
                    annual_parts=annual_parts,
                    annual_name=annual_name,
                    staging_name=staging_name,
                    expected_bytes=expected_bytes,
                    expected_sha=expected_sha,
                )

            if session_uri:
                probe = await self.uploader.query_status(session_uri, expected_bytes)
                if probe.state == "complete":
                    return await self._finish_staged_upload(
                        state=state,
                        finalize=finalize,
                        file_meta=probe.file or {},
                        candidate_id=candidate_obj.id,
                        annual_parts=annual_parts,
                        annual_name=annual_name,
                        staging_name=staging_name,
                        expected_bytes=expected_bytes,
                        expected_sha=expected_sha,
                    )
                if probe.state == "expired":
                    previous = upload.get("previous_canonical_file_id")
                    upload = {"previous_canonical_file_id": previous} if previous else {}
                    finalize["resumable_upload"] = upload
                    session_uri = ""
                else:
                    # Drive is authoritative. A lower offset means rewind and
                    # resend from the server-confirmed position, not failure.
                    upload["offset"] = int(probe.offset)
                    upload["retry_count"] = 0
                    finalize["resumable_upload"] = upload

            if not session_uri:
                # Recovery A: promotion may have succeeded before durable state
                # persistence. If canonical is already exact, do not re-upload.
                canonical = await self.uploader.find_named_file(drive_parent, annual_name)
                previous_canonical_id = str((canonical or {}).get("id") or "") or None
                if previous_canonical_id:
                    canonical_meta = await self.uploader.file_metadata(previous_canonical_id)
                    if self._metadata_matches(canonical_meta, expected_bytes, expected_sha):
                        await self._verify_candidate_and_backup(
                            candidate_id=candidate_obj.id,
                            annual_parts=annual_parts,
                            annual_name=annual_name,
                            expected_bytes=expected_bytes,
                            expected_sha=expected_sha,
                        )
                        return await self._mark_commit_ready(
                            state=state,
                            finalize=finalize,
                            file_id=previous_canonical_id,
                            expected_bytes=expected_bytes,
                            expected_sha=expected_sha,
                            action="report_annual_already_promoted",
                        )

                # Recovery B: upload may have completed but its final response or
                # session URI was lost. Reuse an exact deterministic staging file.
                staged = await self.uploader.find_named_file(drive_parent, staging_name)
                staged_id = str((staged or {}).get("id") or "") or None
                if staged_id:
                    staged_meta = await self.uploader.file_metadata(staged_id)
                    if self._metadata_matches(staged_meta, expected_bytes, expected_sha):
                        upload = {
                            "offset": expected_bytes,
                            "target_file_id": staged_id,
                            "chunk_bytes": self.uploader.chunk_size,
                            "session_broker": "google_apps_script",
                            "staging_name": staging_name,
                            "previous_canonical_file_id": previous_canonical_id,
                            "retry_count": 0,
                            "recovered_completed_staging": True,
                        }
                        finalize["resumable_upload"] = upload
                        state["finalize"] = finalize
                        await self.queue._save(state)
                        return await self._finish_staged_upload(
                            state=state,
                            finalize=finalize,
                            file_meta={"id": staged_id},
                            candidate_id=candidate_obj.id,
                            annual_parts=annual_parts,
                            annual_name=annual_name,
                            staging_name=staging_name,
                            expected_bytes=expected_bytes,
                            expected_sha=expected_sha,
                        )

                session = await self.uploader.start_session(
                    parent_id=drive_parent,
                    name=staging_name,
                    total_bytes=expected_bytes,
                    mime_type="text/csv",
                )
                upload = {
                    "session_uri": session.uri,
                    "offset": 0,
                    "target_file_id": session.file_id,
                    "chunk_bytes": self.uploader.chunk_size,
                    "session_broker": "google_apps_script",
                    "staging_name": staging_name,
                    "previous_canonical_file_id": previous_canonical_id,
                    "retry_count": 0,
                }
                finalize["resumable_upload"] = upload
                state["finalize"] = finalize
                state["status"] = "QUEUED"
                state["last_error"] = None
                await self.queue._save(state)
                await self.queue._schedule(job_id, 0)
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": state["status"],
                    "phase": state.get("phase"),
                    "action": "drive_resumable_session_started",
                    "report_id": report_id,
                    "confirmed_bytes": 0,
                    "annual_bytes": expected_bytes,
                    "canonical_untouched": True,
                }

            offset = max(0, int(upload.get("offset", 0) or 0))
            if offset >= expected_bytes:
                probe = await self.uploader.query_status(session_uri, expected_bytes)
                if probe.state != "complete":
                    if probe.state == "expired":
                        previous = upload.get("previous_canonical_file_id")
                        finalize["resumable_upload"] = (
                            {"previous_canonical_file_id": previous} if previous else {}
                        )
                        state["finalize"] = finalize
                        state["status"] = "QUEUED"
                        await self.queue._save(state)
                        await self.queue._schedule(job_id, 0)
                        return {
                            "ok": True,
                            "job_id": job_id,
                            "status": state["status"],
                            "action": "drive_resumable_session_restarted",
                            "report_id": report_id,
                            "confirmed_bytes": 0,
                            "annual_bytes": expected_bytes,
                        }
                    upload["offset"] = int(probe.offset)
                    finalize["resumable_upload"] = upload
                    state["finalize"] = finalize
                    await self.queue._save(state)
                    await self.queue._schedule(job_id, 0)
                    return {
                        "ok": True,
                        "job_id": job_id,
                        "status": state.get("status", "QUEUED"),
                        "action": "drive_resumable_offset_rewound",
                        "confirmed_bytes": int(probe.offset),
                        "annual_bytes": expected_bytes,
                    }
                return await self._finish_staged_upload(
                    state=state,
                    finalize=finalize,
                    file_meta=probe.file or {},
                    candidate_id=candidate_obj.id,
                    annual_parts=annual_parts,
                    annual_name=annual_name,
                    staging_name=staging_name,
                    expected_bytes=expected_bytes,
                    expected_sha=expected_sha,
                )

            end = min(expected_bytes, offset + self.uploader.chunk_size)
            chunk = await self._read_yandex_range(yandex, candidate_obj.id, offset, end)
            if len(chunk) != end - offset:
                raise RuntimeError("Yandex candidate range returned an unexpected byte count")
            progress = await self.uploader.upload_chunk(
                session_uri=session_uri,
                offset=offset,
                total_bytes=expected_bytes,
                data=chunk,
            )
            if progress.state == "expired":
                previous = upload.get("previous_canonical_file_id")
                finalize["resumable_upload"] = (
                    {"previous_canonical_file_id": previous} if previous else {}
                )
                state["finalize"] = finalize
                state["status"] = "QUEUED"
                state["last_error"] = None
                await self.queue._save(state)
                await self.queue._schedule(job_id, 0)
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": state["status"],
                    "action": "drive_resumable_session_restarted",
                    "report_id": report_id,
                    "confirmed_bytes": 0,
                    "annual_bytes": expected_bytes,
                    "canonical_untouched": True,
                }
            if progress.state == "complete":
                return await self._finish_staged_upload(
                    state=state,
                    finalize=finalize,
                    file_meta=progress.file or {},
                    candidate_id=candidate_obj.id,
                    annual_parts=annual_parts,
                    annual_name=annual_name,
                    staging_name=staging_name,
                    expected_bytes=expected_bytes,
                    expected_sha=expected_sha,
                )
            confirmed = int(progress.offset)
            if confirmed < 0 or confirmed > expected_bytes:
                raise RuntimeError("Drive returned an invalid confirmed upload offset")
            upload["offset"] = confirmed
            upload["retry_count"] = 0
            finalize["resumable_upload"] = upload
            state["finalize"] = finalize
            state["status"] = "QUEUED"
            state["last_error"] = None
            await self.queue._save(state)
            await self.queue._schedule(job_id, 0)
            return {
                "ok": True,
                "job_id": job_id,
                "status": state["status"],
                "phase": state.get("phase"),
                "action": "drive_resumable_chunk_uploaded",
                "report_id": report_id,
                "confirmed_bytes": confirmed,
                "annual_bytes": expected_bytes,
                "progress_percent": round(100.0 * confirmed / expected_bytes, 2),
                "canonical_untouched": True,
            }
        except (ResumableUploadError, ArchiveStorageError) as exc:
            retryable = bool(getattr(exc, "retryable", False))
            current_upload = dict(finalize.get("resumable_upload") or upload)
            promotion_attempted = bool(current_upload.get("promotion_attempted"))
            if not retryable:
                state["status"] = "FAILED"
                state["last_error"] = str(exc)[:1000]
                state["finalize"] = finalize
                await self.queue._save(state)
                await self.queue._unschedule(job_id)
                return {
                    "ok": False,
                    "job_id": job_id,
                    "status": "FAILED",
                    "action": "drive_resumable_upload_failed",
                    "error": state["last_error"],
                    "candidate_preserved": True,
                    "canonical_state": "recheck_required" if promotion_attempted else "untouched",
                }
            retry_count = int(current_upload.get("retry_count", 0) or 0) + 1
            current_upload["retry_count"] = retry_count
            finalize["resumable_upload"] = current_upload
            backoff = self._retry_delay(job_id, retry_count)
            retry_after = int(math.ceil(float(getattr(exc, "retry_after_seconds", 0.0) or 0.0)))
            delay = max(backoff, retry_after)
            state["status"] = "WAITING_RETRY"
            state["last_error"] = str(exc)[:1000]
            state["finalize"] = finalize
            await self.queue._save(state)
            await self.queue._schedule(job_id, delay)
            return {
                "ok": True,
                "job_id": job_id,
                "status": state["status"],
                "action": "drive_resumable_waiting_retry",
                "retry_after_seconds": delay,
                "retry_count": retry_count,
                "report_id": report_id,
                "canonical_state": "recheck_required" if promotion_attempted else "untouched",
            }

    async def _finish_staged_upload(
        self,
        *,
        state: dict[str, Any],
        finalize: dict[str, Any],
        file_meta: dict[str, Any],
        candidate_id: str,
        annual_parts: list[str],
        annual_name: str,
        staging_name: str,
        expected_bytes: int,
        expected_sha: str,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        upload = dict(finalize.get("resumable_upload") or {})
        file_id = str(file_meta.get("id") or upload.get("target_file_id") or upload.get("staged_file_id") or "")
        drive_parent = await self.store.drive.ensure_folder_path(annual_parts)
        if not file_id:
            found = await self.uploader.find_named_file(drive_parent, staging_name)
            file_id = str((found or {}).get("id") or "")
        if not file_id:
            raise RuntimeError("Drive resumable upload completed without a staged file id")

        meta = await self.uploader.file_metadata(file_id)
        if not self._metadata_matches(meta, expected_bytes, expected_sha):
            raise RuntimeError("Drive staged upload failed exact size/SHA256 verification")

        # The canonical Drive file is still untouched at this point unless this
        # is an exact-ID retry after an uncertain promotion response.
        await self._verify_candidate_and_backup(
            candidate_id=candidate_id,
            annual_parts=annual_parts,
            annual_name=annual_name,
            expected_bytes=expected_bytes,
            expected_sha=expected_sha,
        )

        # Persist an explicit promotion checkpoint before the side effect. If the
        # bridge response is lost, the next step can safely re-check/retry using
        # the same staged and previous-canonical file IDs.
        upload["staged_file_id"] = file_id
        upload["promotion_attempted"] = True
        upload["offset"] = expected_bytes
        finalize["resumable_upload"] = upload
        state["finalize"] = finalize
        state["status"] = "PROMOTION_PENDING"
        state["last_error"] = None
        await self.queue._save(state)

        previous_id = str(upload.get("previous_canonical_file_id") or "") or None
        promoted = await self.store.drive.promote_verified_file(
            parent_id=drive_parent,
            file_id=file_id,
            staging_name=staging_name,
            canonical_name=annual_name,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha,
            previous_file_id=previous_id,
        )
        return await self._mark_commit_ready(
            state=state,
            finalize=finalize,
            file_id=promoted.id,
            expected_bytes=expected_bytes,
            expected_sha=expected_sha,
            action="report_annual_uploaded_resumable",
        )

    async def _verify_candidate_and_backup(
        self,
        *,
        candidate_id: str,
        annual_parts: list[str],
        annual_name: str,
        expected_bytes: int,
        expected_sha: str,
    ) -> None:
        yandex = getattr(self.store, "yandex", self.store)
        candidate_data = await yandex.download_bytes(candidate_id)
        if len(candidate_data) != expected_bytes:
            raise RuntimeError("Candidate changed size before Yandex backup mirror")
        if hashlib.sha256(candidate_data).hexdigest() != expected_sha:
            raise RuntimeError("Candidate changed SHA256 before Yandex backup mirror")
        yandex_parent = await yandex.ensure_folder_path(annual_parts)
        backup = await yandex.upload_bytes(
            yandex_parent,
            annual_name,
            candidate_data,
            mime_type="text/csv",
        )
        if getattr(backup, "size", None) is not None and int(backup.size) != expected_bytes:
            raise RuntimeError("Yandex canonical backup failed size verification")

    async def _mark_commit_ready(
        self,
        *,
        state: dict[str, Any],
        finalize: dict[str, Any],
        file_id: str,
        expected_bytes: int,
        expected_sha: str,
        action: str,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        report_id = int(finalize.get("report_id") or 0)
        finalize["annual_object_id"] = str(file_id)
        finalize["phase"] = "COMMIT"
        finalize["resumable_upload_verified"] = {
            "bytes": expected_bytes,
            "sha256": expected_sha,
            "session_broker": "google_apps_script",
            "staged_before_promotion": True,
        }
        finalize.pop("resumable_upload", None)
        state["finalize"] = finalize
        state["status"] = "QUEUED"
        state["last_error"] = None
        await self.queue._save(state)
        await self.queue._schedule(job_id, 0)
        return {
            "ok": True,
            "job_id": job_id,
            "status": state["status"],
            "phase": state.get("phase"),
            "action": action,
            "report_id": report_id,
            "annual_bytes": expected_bytes,
            "annual_sha256": expected_sha,
            "drive_verified": True,
            "backup_verified": True,
            "canonical_promoted": True,
        }

    @staticmethod
    def _metadata_matches(meta: dict[str, Any], expected_bytes: int, expected_sha: str) -> bool:
        try:
            actual_size = int(meta.get("size") or 0)
        except (TypeError, ValueError):
            actual_size = 0
        actual_sha = str(meta.get("sha256Checksum") or "").lower()
        return actual_size == int(expected_bytes) and actual_sha == str(expected_sha).lower()

    @staticmethod
    def _retry_delay(job_id: str, retry_count: int) -> int:
        # Truncated exponential backoff with deterministic 0..5 second jitter.
        exponent = min(max(int(retry_count) - 1, 0), 6)
        base = min(300, 5 * (2 ** exponent))
        jitter = hashlib.sha256(f"{job_id}:{retry_count}".encode("utf-8")).digest()[0] % 6
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
