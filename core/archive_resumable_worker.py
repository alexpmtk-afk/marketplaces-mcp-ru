"""Durable large-file upload step for the marketplace archive queue.

The ordinary queue keeps discovery, WB download, PREPARE and COMMIT unchanged.
Only ``UPLOAD_ANNUAL`` is intercepted here. A single worker invocation starts a
Drive resumable session or transfers at most one bounded chunk, persists the
confirmed offset, and returns. This lets the existing Laser Master candidate
continue without recreating its job or calling WB again.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from .archive_drive_resumable import (
    GoogleDriveResumableUploader,
    ResumableUploadError,
    build_drive_resumable_uploader_from_env,
)
from .archive_queue import JOB_FOLDER, WBFinanceArchiveJobQueue
from .wb_finance_archive import ArchiveLock

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


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
        self.uploader = uploader if uploader is not None else build_drive_resumable_uploader_from_env()

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
        expected_sha = str(finalize.get("annual_sha256") or "")
        if expected_bytes <= 0 or len(expected_sha) != 64:
            raise RuntimeError("Durable annual finalize metadata is incomplete")

        yandex = getattr(self.store, "yandex", self.store)
        drive = getattr(self.store, "drive", None)
        if drive is None:
            raise RuntimeError("Resumable annual upload requires the hybrid Drive/Yandex store")

        candidate_parent = await yandex.ensure_folder_path([*JOB_FOLDER, job_id, "finalize"])
        candidate_name = "annual-candidate.csv"
        candidate_obj = await yandex.find_child(candidate_parent, candidate_name)
        if candidate_obj is None:
            raise RuntimeError("Durable annual finalize candidate is missing")
        if candidate_obj.size is not None and int(candidate_obj.size) != expected_bytes:
            raise RuntimeError("Durable annual finalize candidate failed size verification")

        expected_md5 = str(finalize.get("annual_md5") or "").lower()
        etag = str(getattr(candidate_obj, "etag", None) or "").strip('"').lower()
        if not _MD5_RE.fullmatch(expected_md5):
            if _MD5_RE.fullmatch(etag):
                expected_md5 = etag
            else:
                whole = await yandex.download_bytes(candidate_obj.id)
                if len(whole) != expected_bytes or hashlib.sha256(whole).hexdigest() != expected_sha:
                    raise RuntimeError(
                        "Durable annual finalize candidate failed size/SHA256 verification"
                    )
                expected_md5 = hashlib.md5(whole, usedforsecurity=False).hexdigest()
            finalize["annual_md5"] = expected_md5

        if self.uploader is None:
            state["finalize"] = finalize
            state["status"] = "WAITING_CONFIGURATION"
            state["last_error"] = (
                "Google Drive resumable OAuth is not configured; candidate is preserved"
            )
            await self.queue._save(state)
            await self.queue._unschedule(job_id)
            return {
                "ok": True,
                "job_id": job_id,
                "status": state["status"],
                "phase": state.get("phase"),
                "action": "drive_resumable_oauth_required",
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
        upload = dict(finalize.get("resumable_upload") or {})
        session_uri = str(upload.get("session_uri") or "")

        try:
            if session_uri:
                probe = await self.uploader.query_status(session_uri, expected_bytes)
                if probe.state == "complete":
                    return await self._finish_upload(
                        state, finalize, probe.file or {}, candidate_obj.id,
                        annual_parts, annual_name, expected_bytes, expected_sha, expected_md5,
                    )
                if probe.state == "expired":
                    upload = {}
                    session_uri = ""
                else:
                    upload["offset"] = int(probe.offset)

            if not session_uri:
                session = await self.uploader.start_session(
                    parent_id=drive_parent,
                    name=annual_name,
                    total_bytes=expected_bytes,
                    mime_type="text/csv",
                )
                upload = {
                    "session_uri": session.uri,
                    "offset": 0,
                    "target_file_id": session.file_id,
                    "chunk_bytes": self.uploader.chunk_size,
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
                }

            offset = max(0, int(upload.get("offset", 0) or 0))
            if offset >= expected_bytes:
                probe = await self.uploader.query_status(session_uri, expected_bytes)
                if probe.state != "complete":
                    raise ResumableUploadError(
                        "Drive reports an incomplete session after the expected final offset",
                        retryable=True,
                    )
                return await self._finish_upload(
                    state, finalize, probe.file or {}, candidate_obj.id,
                    annual_parts, annual_name, expected_bytes, expected_sha, expected_md5,
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
                finalize["resumable_upload"] = {}
                state["finalize"] = finalize
                state["status"] = "QUEUED"
                await self.queue._save(state)
                await self.queue._schedule(job_id, 0)
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": state["status"],
                    "action": "drive_resumable_session_expired",
                    "report_id": report_id,
                    "confirmed_bytes": 0,
                    "annual_bytes": expected_bytes,
                }
            if progress.state == "complete":
                return await self._finish_upload(
                    state, finalize, progress.file or {}, candidate_obj.id,
                    annual_parts, annual_name, expected_bytes, expected_sha, expected_md5,
                )
            confirmed = int(progress.offset)
            if confirmed < offset or confirmed > expected_bytes:
                raise RuntimeError("Drive returned an invalid confirmed upload offset")
            upload["offset"] = confirmed
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
            }
        except ResumableUploadError as exc:
            if not exc.retryable:
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
                }
            state["status"] = "WAITING_RETRY"
            state["last_error"] = str(exc)[:1000]
            state["finalize"] = finalize
            await self.queue._save(state)
            await self.queue._schedule(job_id, 15)
            return {
                "ok": True,
                "job_id": job_id,
                "status": state["status"],
                "action": "drive_resumable_waiting_retry",
                "retry_after_seconds": 15,
                "report_id": report_id,
            }

    async def _finish_upload(
        self,
        state: dict[str, Any],
        finalize: dict[str, Any],
        file_meta: dict[str, Any],
        candidate_id: str,
        annual_parts: list[str],
        annual_name: str,
        expected_bytes: int,
        expected_sha: str,
        expected_md5: str,
    ) -> dict[str, Any]:
        job_id = str(state["job_id"])
        report_id = int(finalize.get("report_id") or 0)
        file_id = str(file_meta.get("id") or (finalize.get("resumable_upload") or {}).get("target_file_id") or "")
        if not file_id:
            found = await self.uploader.find_named_file(
                await self.store.drive.ensure_folder_path(annual_parts), annual_name
            )
            file_id = str((found or {}).get("id") or "")
        if not file_id:
            raise RuntimeError("Drive resumable upload completed without a file id")
        meta = await self.uploader.file_metadata(file_id)
        try:
            actual_size = int(meta.get("size") or 0)
        except (TypeError, ValueError):
            actual_size = 0
        actual_md5 = str(meta.get("md5Checksum") or "").lower()
        if actual_size != expected_bytes or actual_md5 != expected_md5:
            raise RuntimeError("Drive resumable upload failed size/MD5 verification")

        # Preserve the existing archive contract: after the canonical Drive file
        # is verified, mirror the exact candidate bytes to the canonical Yandex
        # backup path before committing the registry row.
        yandex = getattr(self.store, "yandex", self.store)
        candidate_data = await yandex.download_bytes(candidate_id)
        if len(candidate_data) != expected_bytes or hashlib.sha256(candidate_data).hexdigest() != expected_sha:
            raise RuntimeError("Candidate changed before Yandex backup mirror")
        yandex_parent = await yandex.ensure_folder_path(annual_parts)
        await yandex.upload_bytes(yandex_parent, annual_name, candidate_data, mime_type="text/csv")

        finalize["annual_object_id"] = file_id
        finalize["phase"] = "COMMIT"
        finalize["resumable_upload_verified"] = {
            "bytes": expected_bytes,
            "md5": expected_md5,
        }
        # Session URIs are bearer-like capabilities. They are no longer needed
        # after verification and must not remain in durable job state.
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
            "action": "report_annual_uploaded_resumable",
            "report_id": report_id,
            "annual_bytes": expected_bytes,
            "annual_sha256": expected_sha,
            "drive_verified": True,
            "backup_verified": True,
        }

    @staticmethod
    async def _read_yandex_range(yandex: Any, file_id: str, start: int, end: int) -> bytes:
        if hasattr(yandex, "download_range"):
            return await yandex.download_range(file_id, start, end)
        # Compatibility with the current Yandex store while v1 is rolled out:
        # use its authenticated request primitive to avoid reading the whole
        # candidate for every chunk.
        response = await yandex._request(
            "GET",
            yandex._object_url(file_id),
            headers={"Range": f"bytes={int(start)}-{int(end) - 1}"},
        )
        return response.content
