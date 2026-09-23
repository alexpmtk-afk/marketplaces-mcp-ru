from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.google_drive_direct import GoogleDriveDirectStore


class ArchiveSourceUnavailable(RuntimeError):
    pass


class GoogleDriveVerifiedCache:
    def __init__(self, store: GoogleDriveDirectStore, cache_root: str | Path):
        self.store = store
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def paths(self, file_id: str, file_name: str, create: bool = True):
        folder = self.cache_root / file_id
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return (
            folder / file_name,
            folder / (file_name + ".part"),
            folder / "metadata.json",
        )

    def _write_metadata(self, path: Path, payload: dict[str, Any]):
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def get_verified_cached(self, file_id: str, file_name: str):
        final, _, metadata_path = self.paths(
            file_id, file_name, create=False
        )

        if not final.exists() or not metadata_path.exists():
            return None

        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        if meta.get("file_id") != file_id:
            return None

        if meta.get("archive_root_id") != self.store.archive_root_id:
            return None

        if meta.get("within_root_verified") is not True:
            return None

        expected_size = int(meta.get("size", -1))
        if final.stat().st_size != expected_size:
            return None

        actual_sha = self.sha256(final)
        if actual_sha != meta.get("sha256"):
            return None

        verified_at = meta.get("verified_at_utc")
        if verified_at:
            dt = datetime.fromisoformat(
                verified_at.replace("Z", "+00:00")
            )
            age = (
                datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
            ).total_seconds()
        else:
            age = max(
                0,
                datetime.now(timezone.utc).timestamp()
                - metadata_path.stat().st_mtime,
            )

        return {
            "path": str(final),
            "sha256": actual_sha,
            "size": expected_size,
            "cache_age_seconds": max(0, int(age)),
            "source": "cache",
            "drive_available": False,
        }

    def sync(
        self,
        file_id: str,
        *,
        expected_sha256: str | None = None,
        force_refresh: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ):
        self.store.assert_within_root(file_id)
        remote = self.store.get_metadata(file_id)

        name = remote["name"]
        expected_size = int(remote.get("size") or 0)

        final, part, metadata_path = self.paths(file_id, name)

        if (
            not force_refresh
            and final.exists()
            and metadata_path.exists()
        ):
            cached = self.get_verified_cached(file_id, name)
            if cached:
                cached["drive_available"] = True
                return cached

        if part.exists() and part.stat().st_size > expected_size:
            part.unlink()

        self.store.download(file_id, part, resume=True)

        actual_size = part.stat().st_size
        if actual_size != expected_size:
            raise ValueError(
                f"SIZE_MISMATCH actual={actual_size} expected={expected_size}"
            )

        actual_sha = self.sha256(part)

        if expected_sha256 and actual_sha != expected_sha256:
            raise ValueError(
                f"SHA256_MISMATCH actual={actual_sha} expected={expected_sha256}"
            )

        os.replace(part, final)

        now = datetime.now(timezone.utc).isoformat()

        self._write_metadata(
            metadata_path,
            {
                "file_id": file_id,
                "name": name,
                "size": expected_size,
                "modified_time": remote.get("modifiedTime"),
                "sha256": actual_sha,
                "verified": True,
                "verified_at_utc": now,
                "archive_root_id": self.store.archive_root_id,
                "within_root_verified": True,
            },
        )

        return {
            "path": str(final),
            "sha256": actual_sha,
            "size": expected_size,
            "cache_age_seconds": 0,
            "source": "drive",
            "drive_available": True,
        }

    def sync_or_cached(self, file_id: str, file_name: str):
        try:
            return self.sync(file_id)
        except Exception as exc:
            cached = self.get_verified_cached(file_id, file_name)
            if cached:
                cached["fallback_reason"] = type(exc).__name__
                return cached

            raise ArchiveSourceUnavailable(
                f"ARCHIVE_SOURCE_UNAVAILABLE file_id={file_id}"
            ) from exc
