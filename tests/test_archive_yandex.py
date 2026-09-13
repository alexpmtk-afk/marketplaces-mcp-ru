from __future__ import annotations

import asyncio

from core.archive_yandex import (
    YandexObjectStorageArchiveStore,
    build_yandex_archive_store_from_env,
)


def test_yandex_archive_is_fail_closed_without_bucket(monkeypatch):
    monkeypatch.delenv("MARKETPLACE_MCP_ARCHIVE_BUCKET", raising=False)
    assert build_yandex_archive_store_from_env() is None


def test_yandex_archive_uses_only_yandex_bucket_env(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_MCP_ARCHIVE_BUCKET", "marketplaces-archive-test")
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_OAUTH_JSON", "must-not-be-read")
    monkeypatch.setenv("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "must-not-be-read")
    store = build_yandex_archive_store_from_env()
    assert store is not None
    assert store.bucket == "marketplaces-archive-test"


def test_object_key_layout_matches_canonical_archive_tree():
    store = YandexObjectStorageArchiveStore(
        bucket="marketplaces-archive-test",
        root_prefix="archive",
    )
    prefix = asyncio.run(store.ensure_folder_path([
        "База данных", "WB", "wb_novokshenov", "2026", "finance", "weekly", "main"
    ]))
    assert prefix == "archive/База данных/WB/wb_novokshenov/2026/finance/weekly/main"
    key = store._join_key(prefix, "wb_novokshenov__weekly_main__2026.csv")
    assert key == (
        "archive/База данных/WB/wb_novokshenov/2026/finance/weekly/main/"
        "wb_novokshenov__weekly_main__2026.csv"
    )
    url = store._object_url(key)
    assert url.startswith("https://storage.yandexcloud.net/marketplaces-archive-test/")
    assert "%D0%91%D0%B0%D0%B7%D0%B0" in url
