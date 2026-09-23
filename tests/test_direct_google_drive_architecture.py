"""Guardrails for the production Direct Google Drive archive selector."""

from __future__ import annotations

import core.archive_hybrid as hybrid


def test_direct_google_drive_backend_is_selected_only_when_explicitly_enabled(
    monkeypatch,
):
    direct_store = object()

    monkeypatch.setenv(
        "MARKETPLACE_MCP_ARCHIVE_DIRECT_GOOGLE",
        "1",
    )
    monkeypatch.setattr(
        hybrid,
        "build_direct_google_archive_store_from_env",
        lambda: direct_store,
    )

    store = hybrid.build_hybrid_archive_store_from_env()

    assert isinstance(
        store,
        hybrid.CanonicalDriveReadOnlyArchiveStore,
    )
    assert store.drive is direct_store


def test_legacy_google_transport_remains_default_rollback_path(
    monkeypatch,
):
    legacy_store = object()

    monkeypatch.delenv(
        "MARKETPLACE_MCP_ARCHIVE_DIRECT_GOOGLE",
        raising=False,
    )
    monkeypatch.setattr(
        hybrid,
        "build_google_archive_store_from_env",
        lambda: legacy_store,
    )
    monkeypatch.setattr(
        hybrid,
        "build_yandex_archive_store_from_env",
        lambda: None,
    )

    store = hybrid.build_hybrid_archive_store_from_env()

    assert isinstance(
        store,
        hybrid.CanonicalDriveReadOnlyArchiveStore,
    )
    assert store.drive is legacy_store
