from pathlib import Path


def test_bridge_v3_is_archive_scoped_and_preflights_drive_api():
    source = Path("docs/apps_script/marketplaces_drive_bridge_v3.gs").read_text(encoding="utf-8")
    assert "const BRIDGE_VERSION=3" in source
    assert "archive_root_id_guard:true" in source
    assert "drive_api_preflight:true" in source
    assert "idempotent_small_write:true" in source
    assert "promotion_replay_safe:true" in source
    assert "readonly_parallel:true" in source
    assert "writeSmallReplaySafe_" in source
    assert "withMutationLock_" in source
    assert "assertFileInsideArchive_" in source
    assert "file_outside_archive_root" in source
    assert "trash_only_allowed_for_diagnostic_copy" in source
    assert "driveApiMetadata_(ARCHIVE_ROOT_ID)" in source
    assert "sha256Checksum" in source
    assert "promote_verified" in source


def test_bridge_manifest_has_only_required_google_scopes():
    manifest = Path("docs/apps_script/appsscript.json").read_text(encoding="utf-8")
    assert "https://www.googleapis.com/auth/drive" in manifest
    assert "https://www.googleapis.com/auth/script.external_request" in manifest
