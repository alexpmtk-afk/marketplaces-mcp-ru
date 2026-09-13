"""Retired Google Drive archive compatibility shim.

Canonical shared archive storage is Yandex Object Storage. This module remains
only so older internal imports cannot silently reactivate Google Drive. All
symbols resolve to the Yandex backend and no Google credentials are read.
"""
from __future__ import annotations

from .archive_yandex import (
    ArchiveObject,
    ArchiveStorageError,
    ArchiveStorageNotConfigured,
    YandexObjectStorageArchiveStore,
    build_yandex_archive_store_from_env,
)

# Temporary compatibility aliases for older type imports. Do not use in new code.
DriveFile = ArchiveObject
GoogleDriveArchiveStore = YandexObjectStorageArchiveStore
build_drive_archive_store_from_env = build_yandex_archive_store_from_env

__all__ = [
    "ArchiveObject",
    "ArchiveStorageError",
    "ArchiveStorageNotConfigured",
    "YandexObjectStorageArchiveStore",
    "build_yandex_archive_store_from_env",
    "DriveFile",
    "GoogleDriveArchiveStore",
    "build_drive_archive_store_from_env",
]
