"""Google Drive archive backend via a project-isolated Google Apps Script bridge.

Production remains on the legacy Marketplaces bridge route until an isolated
Protocol-v1 Marketplaces deployment, secret/Lockbox binding and acceptance are
available. Protocol v1 is opt-in and never falls back to legacy credentials.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

MARKETPLACES_BRIDGE_PROJECT_ID = "marketplaces"
BRIDGE_PROTOCOL_V1 = 1
_LEGACY_MODE = "legacy"
_V1_MODES = {"1", "v1", "protocol-v1"}
_MUTATING_V1_ACTIONS = {
    "write_small",
    "trash_by_id",
    "resumable_start",
    "promote_verified",
}


class ArchiveStorageNotConfigured(RuntimeError):
    """Google Drive archive storage is not configured on the remote MCP."""


class ArchiveStorageError(RuntimeError):
    """The Google Apps Script Drive bridge rejected or failed an operation."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.code = str(code or "").strip() or None


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None = None
    md5_checksum: str | None = None
    sha256_checksum: str | None = None
    modified_time: str | None = None


class GoogleDriveArchiveStore:
    """Async Marketplaces client for legacy Bridge v3 or shared Protocol v1."""

    _MAX_ATTEMPTS = 3
    _RETRYABLE_HTTP_STATUSES = {404, 408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        bridge_url: str,
        bridge_secret: str,
        root_folder_id: str,
        timeout: float = 120.0,
        protocol_version: int = 0,
        project_id: str = MARKETPLACES_BRIDGE_PROJECT_ID,
    ) -> None:
        self.bridge_url = bridge_url.strip()
        self.bridge_secret = bridge_secret.strip()
        self.root_folder_id = root_folder_id.strip()
        self.timeout = float(timeout)
        self.protocol_version = int(protocol_version or 0)
        self.project_id = str(project_id or "").strip()
        if not all((self.bridge_url, self.bridge_secret, self.root_folder_id)):
            raise ArchiveStorageNotConfigured(
                "Google Drive Apps Script bridge URL, secret, or archive root is incomplete"
            )
        if not self.bridge_url.startswith("https://script.google.com/macros/s/"):
            raise ArchiveStorageNotConfigured("Google Drive Apps Script bridge URL is invalid")
        if self.protocol_version not in {0, BRIDGE_PROTOCOL_V1}:
            raise ArchiveStorageNotConfigured(
                f"Unsupported Google Drive bridge protocol: {self.protocol_version}"
            )
        if self.protocol_version == BRIDGE_PROTOCOL_V1 and self.project_id != MARKETPLACES_BRIDGE_PROJECT_ID:
            raise ArchiveStorageNotConfigured(
                "Marketplaces Protocol-v1 client is pinned to project_id=marketplaces"
            )

    @property
    def is_protocol_v1(self) -> bool:
        return self.protocol_version == BRIDGE_PROTOCOL_V1

    @classmethod
    def from_env(cls) -> "GoogleDriveArchiveStore":
        mode = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL", _LEGACY_MODE).strip().lower()
        if mode in _V1_MODES:
            # Deliberately separate variables: Protocol v1 must never silently
            # reuse the legacy deployment or credential during migration.
            url = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL", "").strip()
            secret = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET", "").strip()
            root = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID", "").strip()
            if not url or not secret or not root:
                raise ArchiveStorageNotConfigured(
                    "Protocol v1 requires dedicated MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL, "
                    "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET and "
                    "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID"
                )
            return cls(
                bridge_url=url,
                bridge_secret=secret,
                root_folder_id=root,
                protocol_version=BRIDGE_PROTOCOL_V1,
                project_id=MARKETPLACES_BRIDGE_PROJECT_ID,
            )
        if mode not in {_LEGACY_MODE, "v3", "0", ""}:
            raise ArchiveStorageNotConfigured(
                f"Unknown MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL={mode!r}"
            )
        url = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL", "").strip()
        secret = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET", "").strip()
        root = os.environ.get("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "").strip()
        if not url or not secret or not root:
            raise ArchiveStorageNotConfigured(
                "Set MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL, "
                "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET and "
                "MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID"
            )
        return cls(
            bridge_url=url,
            bridge_secret=secret,
            root_folder_id=root,
            protocol_version=0,
        )

    @staticmethod
    def _path(parts: list[str] | tuple[str, ...]) -> str:
        clean: list[str] = []
        for raw in parts:
            part = str(raw).strip().strip("/")
            if not part:
                continue
            if part in {".", ".."} or "\\" in part:
                raise ArchiveStorageError(f"Invalid Drive archive path segment: {part!r}")
            clean.append(part)
        return "/".join(clean)

    @staticmethod
    def _to_file(item: dict[str, Any]) -> DriveFile:
        try:
            size = int(item["size"]) if item.get("size") is not None else None
        except (TypeError, ValueError):
            size = None
        md5 = str(item.get("md5_checksum") or item.get("md5Checksum") or "").strip() or None
        sha256 = str(item.get("sha256_checksum") or item.get("sha256Checksum") or "").strip() or None
        return DriveFile(
            id=str(item.get("id", "")),
            name=str(item.get("name", "")),
            mime_type=str(item.get("mime_type") or item.get("mimeType") or ""),
            size=size,
            md5_checksum=md5,
            sha256_checksum=sha256,
            modified_time=item.get("modified_time") or item.get("modifiedTime"),
        )

    @staticmethod
    def _stable_idempotency_key(action: str, *parts: Any) -> str:
        material = "\x00".join(str(part) for part in parts)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"marketplaces:{action}:{digest}"

    async def _post_legacy(self, action: str, **payload: Any) -> dict[str, Any]:
        body = {"secret": self.bridge_secret, "action": action, **payload}
        last_error: Exception | None = None
        last_response: httpx.Response | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.post(
                        self.bridge_url,
                        json=body,
                        headers={"Accept": "application/json"},
                    )
                last_response = resp
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._MAX_ATTEMPTS:
                    await asyncio.sleep(0.75 * attempt)
                    continue
                raise ArchiveStorageError(
                    f"Apps Script Drive bridge request failed after {attempt} attempts: {type(exc).__name__}",
                    retryable=True,
                ) from exc
            if resp.is_success:
                break
            retryable = resp.status_code in self._RETRYABLE_HTTP_STATUSES
            if retryable and attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(0.75 * attempt)
                continue
            raise ArchiveStorageError(
                f"Apps Script Drive bridge HTTP {resp.status_code}: {resp.text[:500]}",
                retryable=retryable,
            )
        else:
            if last_error is not None:
                raise ArchiveStorageError(
                    f"Apps Script Drive bridge request failed: {type(last_error).__name__}",
                    retryable=True,
                ) from last_error
            if last_response is not None:
                status = last_response.status_code
                raise ArchiveStorageError(
                    f"Apps Script Drive bridge HTTP {status}: {last_response.text[:500]}",
                    retryable=status in self._RETRYABLE_HTTP_STATUSES,
                )
            raise ArchiveStorageError("Apps Script Drive bridge request failed", retryable=True)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ArchiveStorageError(
                "Apps Script Drive bridge returned a non-JSON response; check web-app access settings"
            ) from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            retryable = bool(data.get("retryable")) if isinstance(data, dict) else False
            raise ArchiveStorageError(
                f"Apps Script Drive bridge rejected {action}: {str(data)[:500]}",
                retryable=retryable,
            )
        return data

    async def _post_v1(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        action = str(action).strip().lower()
        request_id = str(request_id or uuid.uuid4())
        if action in _MUTATING_V1_ACTIONS and not idempotency_key:
            raise ArchiveStorageError(
                f"Protocol v1 mutation {action!r} requires a stable idempotency key",
                code="IDEMPOTENCY_KEY_REQUIRED",
            )
        body: dict[str, Any] = {
            "secret": self.bridge_secret,
            "project_id": MARKETPLACES_BRIDGE_PROJECT_ID,
            "request_id": request_id,
            "action": action,
            "payload": dict(payload or {}),
        }
        if idempotency_key:
            body["idempotency_key"] = str(idempotency_key)

        last_error: Exception | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.post(
                        self.bridge_url,
                        json=body,
                        headers={"Accept": "application/json"},
                    )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._MAX_ATTEMPTS:
                    await asyncio.sleep(0.75 * attempt)
                    continue
                raise ArchiveStorageError(
                    f"Bridge v1 transport failed after {attempt} attempts: {type(exc).__name__}",
                    retryable=True,
                    code="TRANSPORT_ERROR",
                ) from exc

            if resp.status_code in self._RETRYABLE_HTTP_STATUSES and attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(0.75 * attempt)
                continue
            if not resp.is_success:
                raise ArchiveStorageError(
                    f"Bridge v1 HTTP {resp.status_code}",
                    retryable=resp.status_code in self._RETRYABLE_HTTP_STATUSES,
                    code="HTTP_ERROR",
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise ArchiveStorageError(
                    "Bridge v1 returned a non-JSON response",
                    code="NON_JSON_RESPONSE",
                ) from exc
            if not isinstance(data, dict):
                raise ArchiveStorageError(
                    "Bridge v1 response is not an object",
                    code="INVALID_RESPONSE",
                )
            if int(data.get("protocol_version") or 0) != BRIDGE_PROTOCOL_V1:
                raise ArchiveStorageError(
                    "Bridge v1 protocol mismatch",
                    code="PROTOCOL_MISMATCH",
                )
            if str(data.get("project_id") or "") != MARKETPLACES_BRIDGE_PROJECT_ID:
                raise ArchiveStorageError(
                    "Bridge v1 project mismatch",
                    code="PROJECT_MISMATCH",
                )
            if str(data.get("request_id") or "") != request_id:
                raise ArchiveStorageError(
                    "Bridge v1 request_id mismatch",
                    code="REQUEST_ID_MISMATCH",
                )
            echoed_action = str(data.get("action") or "")
            if echoed_action and echoed_action != action:
                raise ArchiveStorageError(
                    "Bridge v1 action mismatch",
                    code="ACTION_MISMATCH",
                )
            if data.get("ok") is True:
                result = data.get("result")
                return dict(result) if isinstance(result, dict) else {}

            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(err.get("code") or "BRIDGE_REJECTED")
            message = str(err.get("message") or "bridge rejected request")
            retryable = bool(err.get("retryable"))
            if retryable and attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(0.75 * attempt)
                continue
            raise ArchiveStorageError(
                f"Bridge v1 {code}: {message}",
                retryable=retryable,
                code=code,
            )

        raise ArchiveStorageError(
            f"Bridge v1 transport failed: {type(last_error).__name__ if last_error else 'unknown'}",
            retryable=True,
            code="TRANSPORT_ERROR",
        )

    async def _call(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if self.is_protocol_v1:
            return await self._post_v1(
                action,
                payload,
                idempotency_key=idempotency_key,
            )
        return await self._post_legacy(action, **dict(payload or {}))

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        return self._path(parts)

    async def find_child(
        self,
        parent_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> DriveFile | None:
        data = await self._call(
            "stat",
            {"path": self._path((parent_id,)), "filename": str(name)},
        )
        if not data.get("found"):
            return None
        item = self._to_file(dict(data.get("file") or {}))
        if mime_type and item.mime_type and item.mime_type != mime_type:
            return None
        return item

    async def file_metadata(self, file_id: str) -> dict[str, Any]:
        data = await self._call("metadata_by_id", {"file_id": str(file_id)})
        item = self._to_file(dict(data.get("file") or {}))
        if not item.id:
            raise ArchiveStorageError("Apps Script Drive bridge metadata returned no file id")
        return {
            "id": item.id,
            "name": item.name,
            "size": item.size,
            "md5Checksum": item.md5_checksum,
            "sha256Checksum": item.sha256_checksum,
            "mimeType": item.mime_type,
            "modifiedTime": item.modified_time,
        }

    async def trash_file(self, file_id: str) -> None:
        idem = None
        if self.is_protocol_v1:
            idem = self._stable_idempotency_key("trash_by_id", str(file_id))
        data = await self._call(
            "trash_by_id",
            {"file_id": str(file_id)},
            idempotency_key=idem,
        )
        if data.get("trashed") is not True:
            raise ArchiveStorageError("Apps Script Drive bridge failed to trash file")

    async def promote_verified_file(
        self,
        *,
        parent_id: str,
        file_id: str,
        staging_name: str,
        canonical_name: str,
        expected_bytes: int,
        expected_sha256: str,
        previous_file_id: str | None = None,
    ) -> DriveFile:
        effective_staging_name = str(staging_name)
        if self.is_protocol_v1:
            # Bridge v1 derives its physical staging filename from the stable
            # idempotency key. Existing Marketplaces workers may still carry a
            # logical legacy staging name, so promote using Drive's actual name.
            current = await self.file_metadata(str(file_id))
            if str(current.get("name") or "") and str(current.get("name")) != str(canonical_name):
                effective_staging_name = str(current["name"])
        payload = {
            "path": self._path((parent_id,)),
            "file_id": str(file_id),
            "staging_filename": effective_staging_name,
            "canonical_filename": str(canonical_name),
            "expected_bytes": int(expected_bytes),
            "expected_sha256": str(expected_sha256).lower(),
            "previous_file_id": str(previous_file_id or ""),
        }
        idem = None
        if self.is_protocol_v1:
            idem = self._stable_idempotency_key(
                "promote_verified",
                payload["path"],
                payload["file_id"],
                payload["canonical_filename"],
                payload["expected_bytes"],
                payload["expected_sha256"],
                payload["previous_file_id"],
            )
        data = await self._call(
            "promote_verified",
            payload,
            idempotency_key=idem,
        )
        item = self._to_file(dict(data.get("file") or {}))
        if not item.id:
            raise ArchiveStorageError("Apps Script Drive bridge promotion returned no file id")
        if item.id != str(file_id):
            raise ArchiveStorageError("Apps Script Drive bridge promoted an unexpected file id")
        if item.name != str(canonical_name):
            raise ArchiveStorageError("Apps Script Drive bridge promotion returned the wrong canonical name")
        if item.size != int(expected_bytes) or item.sha256_checksum != str(expected_sha256).lower():
            raise ArchiveStorageError(
                "Apps Script Drive bridge promotion failed final size/SHA256 verification"
            )
        if (
            previous_file_id
            and str(previous_file_id) != str(file_id)
            and data.get("previous_file_trashed") is not True
        ):
            raise ArchiveStorageError(
                "Apps Script Drive bridge did not confirm previous canonical cleanup",
                retryable=True,
            )
        return item

    async def start_resumable_session(
        self,
        *,
        parent_id: str,
        name: str,
        total_bytes: int,
        mime_type: str = "text/csv",
    ) -> dict[str, Any]:
        payload = {
            "path": self._path((parent_id,)),
            "filename": str(name),
            "mime_type": str(mime_type),
            "total_bytes": int(total_bytes),
        }
        idem = None
        if self.is_protocol_v1:
            idem = self._stable_idempotency_key(
                "resumable_start",
                payload["path"],
                payload["filename"],
                payload["mime_type"],
                payload["total_bytes"],
            )
        data = await self._call(
            "resumable_start",
            payload,
            idempotency_key=idem,
        )
        session_uri = str(data.get("session_uri") or "").strip()
        if not session_uri:
            raise ArchiveStorageError("Apps Script Drive bridge returned no resumable session URI")
        return {
            "session_uri": session_uri,
            "file_id": str(data.get("file_id") or "").strip() or None,
            "staging_filename": str(data.get("staging_filename") or "").strip() or None,
        }

    async def start_large_download(self, file_id: str) -> dict[str, Any]:
        if not self.is_protocol_v1:
            raise ArchiveStorageError(
                "Large-download capability is only defined by shared Bridge Protocol v1",
                code="LARGE_DOWNLOAD_PROTOCOL_REQUIRED",
            )
        return await self._post_v1(
            "large_download_start",
            {"file_id": str(file_id)},
        )

    async def download_bytes(self, file_id: str) -> bytes:
        if self.is_protocol_v1:
            # Protocol v1 intentionally has no whole-file Base64 read-by-id.
            # Keep fail-closed until the shared large-download result contract is
            # implemented and accepted by the common bridge project.
            await self.start_large_download(str(file_id))
            raise ArchiveStorageError(
                "Bridge v1 large-download transport result is not yet accepted by Marketplaces",
                code="LARGE_READ_NOT_READY",
            )
        data = await self._post_legacy("read_by_id", file_id=str(file_id))
        encoded = str(data.get("content_base64", ""))
        if not encoded:
            return b""
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ArchiveStorageError("Apps Script Drive bridge returned invalid base64") from exc

    async def download_named(
        self,
        parent_id: str,
        name: str,
    ) -> tuple[DriveFile | None, bytes | None]:
        if self.is_protocol_v1:
            try:
                data = await self._post_v1(
                    "read_small",
                    {"path": self._path((parent_id,)), "filename": str(name)},
                )
            except ArchiveStorageError as exc:
                if exc.code != "LARGE_READ_REQUIRED":
                    raise
                item = await self.find_child(parent_id, name)
                if item is None:
                    return None, None
                await self.start_large_download(item.id)
                raise ArchiveStorageError(
                    "Bridge v1 large-download transport result is not yet accepted by Marketplaces",
                    code="LARGE_READ_NOT_READY",
                )
            if not data.get("found"):
                return None, None
            item = self._to_file(dict(data.get("file") or {}))
            encoded = str(data.get("content_base64", ""))
            try:
                raw = base64.b64decode(encoded, validate=True) if encoded else b""
            except ValueError as exc:
                raise ArchiveStorageError(
                    "Bridge v1 returned invalid small-read base64",
                    code="INVALID_BASE64",
                ) from exc
            expected_sha = str(data.get("sha256") or item.sha256_checksum or "").lower()
            actual_sha = hashlib.sha256(raw).hexdigest()
            if expected_sha and expected_sha != actual_sha:
                raise ArchiveStorageError(
                    "Bridge v1 small-read SHA256 mismatch",
                    code="SHA256_MISMATCH",
                )
            if item.size is not None and len(raw) != item.size:
                raise ArchiveStorageError(
                    f"Bridge v1 small-read size mismatch for {name!r}",
                    code="SIZE_MISMATCH",
                )
            return item, raw

        data = await self._post_legacy(
            "read",
            path=self._path((parent_id,)),
            filename=str(name),
        )
        if not data.get("found"):
            return None, None
        item = self._to_file(dict(data.get("file") or {}))
        encoded = str(data.get("content_base64", ""))
        try:
            raw = base64.b64decode(encoded, validate=True) if encoded else b""
        except ValueError as exc:
            raise ArchiveStorageError("Apps Script Drive bridge returned invalid base64") from exc
        if item.size is not None and len(raw) != item.size:
            raise ArchiveStorageError(
                f"Apps Script Drive bridge size mismatch for {name!r}: {len(raw)} != {item.size}"
            )
        return item, raw

    async def upload_bytes(
        self,
        parent_id: str,
        name: str,
        data: bytes,
        *,
        mime_type: str = "text/csv",
    ) -> DriveFile:
        sha256 = hashlib.sha256(data).hexdigest()
        path = self._path((parent_id,))
        if self.is_protocol_v1:
            payload = {
                "path": path,
                "filename": str(name),
                "mime_type": str(mime_type),
                "content_base64": base64.b64encode(data).decode("ascii"),
                "sha256": sha256,
            }
            idem = self._stable_idempotency_key(
                "write_small",
                path,
                str(name),
                str(mime_type),
                len(data),
                sha256,
            )
            result = await self._post_v1(
                "write_small",
                payload,
                idempotency_key=idem,
            )
        else:
            result = await self._post_legacy(
                "write",
                path=path,
                filename=str(name),
                mime_type=mime_type,
                content_base64=base64.b64encode(data).decode("ascii"),
                sha256=sha256,
            )
        returned_sha = str(result.get("sha256", "")).lower()
        if returned_sha and returned_sha != sha256:
            raise ArchiveStorageError(f"Apps Script Drive bridge checksum mismatch for {name!r}")
        item = self._to_file(dict(result.get("file") or {}))
        if not item.id:
            raise ArchiveStorageError("Apps Script Drive bridge upload returned no file id")
        if item.size is not None and item.size != len(data):
            raise ArchiveStorageError(f"Apps Script Drive bridge uploaded size mismatch for {name!r}")
        if self.is_protocol_v1 and item.sha256_checksum and item.sha256_checksum.lower() != sha256:
            raise ArchiveStorageError(
                f"Bridge v1 uploaded SHA256 mismatch for {name!r}",
                code="SHA256_MISMATCH",
            )
        return item

    async def status(self) -> dict[str, Any]:
        if self.is_protocol_v1:
            data = await self._post_v1("health")
            actual_root = str(data.get("root_id", ""))
            root_name = str(data.get("root_name", ""))
            capabilities = dict(data.get("capabilities") or {})
            if int(data.get("protocol_version") or 0) != BRIDGE_PROTOCOL_V1:
                raise ArchiveStorageError(
                    "Bridge v1 deep health protocol mismatch",
                    code="PROTOCOL_MISMATCH",
                )
            if str(data.get("project_id") or "") != MARKETPLACES_BRIDGE_PROJECT_ID:
                raise ArchiveStorageError(
                    "Bridge v1 deep health project mismatch",
                    code="PROJECT_MISMATCH",
                )
            if actual_root != self.root_folder_id:
                raise ArchiveStorageError(
                    f"Bridge v1 root mismatch: {actual_root!r} != {self.root_folder_id!r}",
                    code="ROOT_MISMATCH",
                )
            if capabilities.get("fixed_root_file_id_guard") is not True:
                raise ArchiveStorageError(
                    "Bridge v1 does not advertise fixed-root file-id protection",
                    code="ROOT_GUARD_REQUIRED",
                )
            if capabilities.get("idempotent_mutations") is not True:
                raise ArchiveStorageError(
                    "Bridge v1 does not advertise idempotent mutations",
                    code="IDEMPOTENCY_REQUIRED",
                )
            return {
                "configured": True,
                "reachable": True,
                "backend": "google_drive_bridge_v1",
                "root_folder_id": actual_root,
                "root_name": root_name,
                "bridge_protocol_version": BRIDGE_PROTOCOL_V1,
                "bridge_release": data.get("bridge_release"),
                "project_id": MARKETPLACES_BRIDGE_PROJECT_ID,
                "capabilities": capabilities,
                "large_download_ready": capabilities.get("drive_large_download") is True,
            }

        data = await self._post_legacy("health")
        actual_root = str(data.get("root_id", ""))
        root_name = str(data.get("root_name", ""))
        if actual_root != self.root_folder_id:
            raise ArchiveStorageError(
                f"Apps Script Drive bridge root mismatch: {actual_root!r} != {self.root_folder_id!r}"
            )
        return {
            "configured": True,
            "reachable": True,
            "backend": "google_drive_apps_script_bridge",
            "root_folder_id": actual_root,
            "root_name": root_name,
            "bridge_version": data.get("version"),
            "bridge_protocol_mode": "legacy",
        }


def build_google_archive_store_from_env() -> GoogleDriveArchiveStore | None:
    try:
        return GoogleDriveArchiveStore.from_env()
    except ArchiveStorageNotConfigured:
        return None
