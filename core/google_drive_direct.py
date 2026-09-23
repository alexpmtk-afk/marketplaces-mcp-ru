from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account


class GoogleDriveDirectStore:
    """
    Read-only Google Drive backend for Marketplaces archive.

    No create/update/delete methods are intentionally exposed.
    """

    API = "https://www.googleapis.com/drive/v3"

    def __init__(
        self,
        *,
        credential_path: str,
        archive_root_id: str,
        timeout: int = 60,
    ) -> None:
        self.credential_path = credential_path
        self.archive_root_id = archive_root_id
        self.timeout = timeout

        self._credentials = (
            service_account.Credentials.from_service_account_file(
                credential_path,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
        )

    def _headers(self) -> dict[str, str]:
        if not self._credentials.valid:
            self._credentials.refresh(Request())

        return {
            "Authorization": f"Bearer {self._credentials.token}",
        }

    def get_metadata(self, file_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.API}/files/{file_id}",
            headers=self._headers(),
            params={
                "supportsAllDrives": "true",
                "fields": (
                    "id,name,mimeType,size,modifiedTime,parents,"
                    "md5Checksum,capabilities"
                ),
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        self.assert_within_root(parent_id)

        items: list[dict[str, Any]] = []
        token: str | None = None

        while True:
            params = {
                "q": f"'{parent_id}' in parents and trashed = false",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": (
                    "nextPageToken,"
                    "files(id,name,mimeType,size,modifiedTime,parents,md5Checksum)"
                ),
            }
            if token:
                params["pageToken"] = token

            response = requests.get(
                f"{self.API}/files",
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()

            payload = response.json()
            items.extend(payload.get("files", []))

            token = payload.get("nextPageToken")
            if not token:
                return items

    def is_within_root(self, file_id: str) -> bool:
        if file_id == self.archive_root_id:
            return True

        visited: set[str] = set()
        pending = [file_id]

        while pending:
            current = pending.pop()

            if current in visited:
                continue
            visited.add(current)

            if current == self.archive_root_id:
                return True

            try:
                meta = self.get_metadata(current)
            except requests.HTTPError:
                return False

            for parent in meta.get("parents", []):
                if parent == self.archive_root_id:
                    return True
                if parent not in visited:
                    pending.append(parent)

        return False

    def assert_within_root(self, file_id: str) -> None:
        if not self.is_within_root(file_id):
            raise PermissionError(
                f"Drive item is outside ARCHIVE_ROOT_ID: {file_id}"
            )

    def download(
        self,
        file_id: str,
        destination: str | Path,
        *,
        resume: bool = True,
    ) -> Path:
        self.assert_within_root(file_id)

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        headers = self._headers()
        mode = "wb"

        if resume and destination.exists():
            offset = destination.stat().st_size
            if offset > 0:
                headers = dict(headers)
                headers["Range"] = f"bytes={offset}-"
                mode = "ab"

        response = requests.get(
            f"{self.API}/files/{file_id}",
            headers=headers,
            params={
                "alt": "media",
                "supportsAllDrives": "true",
            },
            stream=True,
            timeout=self.timeout,
        )

        if response.status_code == 416 and destination.exists():
            return destination

        response.raise_for_status()

        # Сервер не принял Range и вернул полный файл:
        # не дописываем его поверх существующего partial.
        if mode == "ab" and response.status_code == 200:
            mode = "wb"

        with destination.open(mode) as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

        return destination


def from_marketplaces_environment() -> GoogleDriveDirectStore:
    return GoogleDriveDirectStore(
        credential_path=os.getenv(
            "GOOGLE_DRIVE_CREDENTIAL",
            "/opt/mcp/secrets/marketplaces/google-drive-reader.json",
        ),
        archive_root_id=os.getenv(
            "ARCHIVE_ROOT_ID",
            "1UVKUcFfDhCDk6nMHX1mWg9cRL05DT-OJ",  # pragma: allowlist secret
        ),
    )
