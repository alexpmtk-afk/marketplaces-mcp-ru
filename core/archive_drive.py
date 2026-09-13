"""Compatibility imports for canonical Google Drive archive storage."""
from __future__ import annotations

from .archive_google import (
    ArchiveStorageError,
    ArchiveStorageNotConfigured,
    DriveFile,
    GoogleDriveArchiveStore,
    build_google_archive_store_from_env,
)

build_drive_archive_store_from_env = build_google_archive_store_from_env

__all__ = [
    "ArchiveStorageError",
    "ArchiveStorageNotConfigured",
    "DriveFile",
    "GoogleDriveArchiveStore",
    "build_google_archive_store_from_env",
    "build_drive_archive_store_from_env",
]
