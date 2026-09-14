"""Safe diagnostic resumable upload for an existing annual candidate.

This path never advances archive progress and never writes the canonical annual
filename. It uploads the already-prepared candidate to a temporary Drive name,
verifies size + SHA256 from Drive metadata, trashes the diagnostic copy, and
persists only diagnostic state on the existing durable job.
"""
from __future__ import annotations

from typing import Any

from .archive_drive_resumable import GoogleDriveResumableUploader, ResumableUploadError, build_drive_resumable_uploader_from_bridge
from .archive_queue import JOB_FOLDER, WBFinanceArchiveJobQueue
from .wb_finance_archive import ArchiveLock


class WBFinanceResumableDiagnostic:
    def __init__(self, queue: WBFinanceArchiveJobQueue, store: Any, uploader: GoogleDriveResumableUploader | None = None) -> None:
        self.queue = queue
        self.store = store
        drive = getattr(store, "drive", None)
        self.uploader = uploader if uploader is not None else (build_drive_resumable_uploader_from_bridge(drive) if drive is not None else None)

    async def step(self, job_id: str) -> dict[str, Any]:
        selected = str(job_id).strip()
        if not selected:
            return {"ok": False, "error": "job_id_required"}
        async with ArchiveLock(key=f"marketplace-archive:v2:wb-finance:job:{selected}", ttl_seconds=900):
            state = await self.queue._load(selected)
            if state is None:
                return {"ok": False, "error": "archive_job_not_found", "job_id": selected}
            finalize = dict(state.get("finalize") or {})
            if str(finalize.get("phase") or "") != "UPLOAD_ANNUAL":
                return {"ok": False, "error": "diagnostic_requires_upload_annual", "job_id": selected, "phase": finalize.get("phase"), "completed_count": state.get("completed_count")}
            return await self._step_locked(state, finalize)

    async def _step_locked(self, state: dict[str, Any], finalize: dict[str, Any]) -> dict[str, Any]:
        job_id = str(state["job_id"])
        report_id = int(finalize.get("report_id") or 0)
        expected_bytes = int(finalize.get("annual_bytes") or 0)
        expected_sha = str(finalize.get("annual_sha256") or "").lower()
        if expected_bytes <= 0 or len(expected_sha) != 64:
            raise RuntimeError("Durable annual finalize metadata is incomplete")
        if self.uploader is None:
            return {"ok": False, "error": "drive_resumable_bridge_required", "job_id": job_id, "candidate_preserved": True}

        yandex = getattr(self.store, "yandex", self.store)
        drive = getattr(self.store, "drive", None)
        if drive is None:
            raise RuntimeError("Diagnostic requires hybrid Drive/Yandex archive store")
        candidate_parent = await yandex.ensure_folder_path([*JOB_FOLDER, job_id, "finalize"])
        candidate = await yandex.find_child(candidate_parent, "annual-candidate.csv")
        if candidate is None:
            raise RuntimeError("Durable annual finalize candidate is missing")
        if candidate.size is not None and int(candidate.size) != expected_bytes:
            raise RuntimeError("Durable annual candidate failed size verification")

        diag = dict(finalize.get("resumable_diagnostic") or {})
        if diag.get("status") == "PASS" and diag.get("candidate_sha256") == expected_sha:
            return {"ok": True, "job_id": job_id, "action": "diagnostic_pass", "report_id": report_id, "completed_count": state.get("completed_count"), "candidate_sha256": expected_sha, "trashed": bool(diag.get("trashed"))}

        cabinet = str(state["cabinet"])
        year = int(state["year"])
        annual_parts = ["База данных", "WB", cabinet, str(year), "finance", "weekly", "main"]
        drive_parent = await drive.ensure_folder_path(annual_parts)
        temp_name = f".{cabinet}__weekly_main__{year}.diagnostic-report-{report_id}.csv"
        session_uri = str(diag.get("session_uri") or "")

        if session_uri:
            probe = await self.uploader.query_status(session_uri, expected_bytes)
            if probe.state == "complete":
                return await self._verify_and_cleanup(state, finalize, probe.file or {})
            if probe.state == "expired":
                diag = {}
                session_uri = ""
            else:
                diag["offset"] = int(probe.offset)

        if not session_uri:
            session = await self.uploader.start_session(parent_id=drive_parent, name=temp_name, total_bytes=expected_bytes, mime_type="text/csv")
            diag = {"status": "UPLOADING", "session_uri": session.uri, "offset": 0, "target_file_id": session.file_id, "candidate_sha256": expected_sha, "temp_name": temp_name}
            finalize["resumable_diagnostic"] = diag
            state["finalize"] = finalize
            await self.queue._save(state)
            return {"ok": True, "job_id": job_id, "action": "diagnostic_session_started", "report_id": report_id, "completed_count": state.get("completed_count"), "confirmed_bytes": 0, "annual_bytes": expected_bytes}

        offset = max(0, int(diag.get("offset") or 0))
        end = min(expected_bytes, offset + self.uploader.chunk_size)
        if offset >= expected_bytes:
            probe = await self.uploader.query_status(session_uri, expected_bytes)
            if probe.state != "complete":
                raise ResumableUploadError("Diagnostic session is incomplete after expected final offset", retryable=True)
            return await self._verify_and_cleanup(state, finalize, probe.file or {})
        chunk = await self._read_range(yandex, candidate.id, offset, end)
        if len(chunk) != end - offset:
            raise RuntimeError("Yandex candidate range returned an unexpected byte count")
        progress = await self.uploader.upload_chunk(session_uri=session_uri, offset=offset, total_bytes=expected_bytes, data=chunk)
        if progress.state == "expired":
            diag = {"status": "RESTART_REQUIRED", "candidate_sha256": expected_sha, "temp_name": temp_name}
            finalize["resumable_diagnostic"] = diag
            state["finalize"] = finalize
            await self.queue._save(state)
            return {"ok": True, "job_id": job_id, "action": "diagnostic_session_expired", "report_id": report_id}
        if progress.state == "complete":
            return await self._verify_and_cleanup(state, finalize, progress.file or {})
        diag["offset"] = int(progress.offset)
        finalize["resumable_diagnostic"] = diag
        state["finalize"] = finalize
        await self.queue._save(state)
        return {"ok": True, "job_id": job_id, "action": "diagnostic_chunk_uploaded", "report_id": report_id, "completed_count": state.get("completed_count"), "confirmed_bytes": int(progress.offset), "annual_bytes": expected_bytes}

    async def _verify_and_cleanup(self, state: dict[str, Any], finalize: dict[str, Any], completion_meta: dict[str, Any]) -> dict[str, Any]:
        job_id = str(state["job_id"])
        report_id = int(finalize.get("report_id") or 0)
        expected_bytes = int(finalize.get("annual_bytes") or 0)
        expected_sha = str(finalize.get("annual_sha256") or "").lower()
        diag = dict(finalize.get("resumable_diagnostic") or {})
        file_id = str(completion_meta.get("id") or diag.get("target_file_id") or "")
        if not file_id:
            raise RuntimeError("Diagnostic upload completed without a Drive file id")
        meta = await self.uploader.file_metadata(file_id)
        try:
            actual_size = int(meta.get("size") or 0)
        except (TypeError, ValueError):
            actual_size = 0
        actual_sha = str(meta.get("sha256Checksum") or "").lower()
        if actual_size != expected_bytes or actual_sha != expected_sha:
            diag.update({"status": "FAILED_VERIFY", "target_file_id": file_id, "actual_bytes": actual_size, "actual_sha256": actual_sha, "candidate_sha256": expected_sha})
            finalize["resumable_diagnostic"] = diag
            state["finalize"] = finalize
            await self.queue._save(state)
            return {"ok": False, "job_id": job_id, "action": "diagnostic_verify_failed", "report_id": report_id, "expected_bytes": expected_bytes, "actual_bytes": actual_size, "sha256_match": actual_sha == expected_sha, "candidate_preserved": True}
        await self.store.drive.trash_file(file_id)
        diag = {"status": "PASS", "candidate_sha256": expected_sha, "verified_bytes": expected_bytes, "verified_sha256": actual_sha, "target_file_id": file_id, "trashed": True}
        finalize["resumable_diagnostic"] = diag
        state["finalize"] = finalize
        await self.queue._save(state)
        return {"ok": True, "job_id": job_id, "action": "diagnostic_pass", "report_id": report_id, "completed_count": state.get("completed_count"), "verified_bytes": expected_bytes, "verified_sha256": actual_sha, "trashed": True}

    @staticmethod
    async def _read_range(yandex: Any, file_id: str, start: int, end: int) -> bytes:
        if hasattr(yandex, "download_range"):
            return await yandex.download_range(file_id, start, end)
        response = await yandex._request("GET", yandex._object_url(file_id), headers={"Range": f"bytes={int(start)}-{int(end)-1}"})
        return response.content
