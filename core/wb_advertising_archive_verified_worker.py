"""Recovery hardening for the WB Advertising Archive commit worker."""
from __future__ import annotations

import hashlib
from typing import Any

from .wb_advertising_archive_worker import WBAdvertisingArchiveWorker, _metadata_matches


class VerifiedWBAdvertisingArchiveWorker(WBAdvertisingArchiveWorker):
    """Require canonical Drive bytes and Yandex backup before coverage commit.

    The base worker already writes backup before normal promotion. This wrapper
    closes the crash-recovery path: if Drive is already canonical on retry, the
    Yandex backup is verified/recreated before the job may advance to COVERAGE.
    """

    async def _ensure_backup(self, commit: dict[str, Any]) -> None:
        expected_bytes = int(commit.get("annual_bytes", 0) or 0)
        expected_sha = str(commit.get("annual_sha256") or "").lower()
        candidate_id = str(commit.get("candidate_id") or "")
        annual_parts = [str(item) for item in commit.get("annual_parts") or []]
        annual_name = str(commit.get("annual_name") or "")
        if expected_bytes <= 0 or len(expected_sha) != 64 or not candidate_id or not annual_parts or not annual_name:
            raise RuntimeError("Advertising backup recovery metadata is incomplete")
        yandex = getattr(self.store, "yandex", self.store)
        candidate = await yandex.download_bytes(candidate_id)
        if len(candidate) != expected_bytes or hashlib.sha256(candidate).hexdigest() != expected_sha:
            raise RuntimeError("Advertising candidate failed recovery SHA256 verification")
        parent = await yandex.ensure_folder_path(annual_parts)
        backup = await yandex.upload_bytes(parent, annual_name, candidate, mime_type="text/csv")
        if getattr(backup, "size", None) is not None and int(backup.size) != expected_bytes:
            raise RuntimeError("Advertising recovery backup failed size verification")
        verified = await yandex.download_bytes(str(backup.id))
        if len(verified) != expected_bytes or hashlib.sha256(verified).hexdigest() != expected_sha:
            raise RuntimeError("Advertising recovery backup failed exact SHA256 verification")

    async def _upload_dataset(
        self,
        state: dict[str, Any],
        commit: dict[str, Any],
        dataset: str,
    ) -> dict[str, Any]:
        if self.uploader is not None:
            expected_bytes = int(commit.get("annual_bytes", 0) or 0)
            expected_sha = str(commit.get("annual_sha256") or "").lower()
            annual_parts = [str(item) for item in commit.get("annual_parts") or []]
            annual_name = str(commit.get("annual_name") or "")
            if expected_bytes > 0 and len(expected_sha) == 64 and annual_parts and annual_name:
                drive = getattr(self.store, "drive", None)
                if drive is not None:
                    drive_parent = await drive.ensure_folder_path(annual_parts)
                    canonical = await self.uploader.find_named_file(drive_parent, annual_name)
                    canonical_id = str((canonical or {}).get("id") or "")
                    if canonical_id:
                        meta = await self.uploader.file_metadata(canonical_id)
                        if _metadata_matches(meta, expected_bytes, expected_sha):
                            await self._ensure_backup(commit)
                            job_id = str(state["job_id"])
                            commit["canonical_file_id"] = canonical_id
                            commit["dataset_phase"] = "COVERAGE"
                            state["commit"] = commit
                            state["status"] = "COMMITTING"
                            state["last_error"] = None
                            await self.queue._save(state)
                            await self.queue._schedule(job_id, 0)
                            return {
                                "ok": True,
                                "job_id": job_id,
                                "action": "canonical_and_backup_recovered",
                                "dataset": dataset,
                                "drive_verified": True,
                                "backup_verified": True,
                            }
        return await super()._upload_dataset(state, commit, dataset)
