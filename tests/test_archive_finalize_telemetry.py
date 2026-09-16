import json

from core.archive_finalize_telemetry import (
    ensure_finalize_telemetry,
    finalize_telemetry_snapshot,
    public_finalize_telemetry,
    record_finalize_event,
    sync_resumable_telemetry,
)


def test_telemetry_is_stable_and_never_exposes_resumable_session_uri():
    state = {"job_id": "wb-finance-wb_laser_master-2026"}
    finalize = {
        "report_id": 751229976,
        "phase": "UPLOAD_ANNUAL",
        "annual_bytes": 36269883,
        "annual_sha256": "a" * 64,
        "resumable_upload": {
            "session_uri": "https://www.googleapis.com/upload/drive/SECRET-BEARER-CAPABILITY",
            "offset": 4194304,
            "target_file_id": "stage-1",
            "previous_canonical_file_id": "old-1",
        },
    }
    state["finalize"] = finalize
    first = ensure_finalize_telemetry(state, finalize, candidate_object_id="candidate-1")
    correlation = first["correlation_id"]
    sync_resumable_telemetry(state, finalize, candidate_object_id="candidate-1")
    record_finalize_event(
        state, finalize, "RESUMABLE_CHUNK_CONFIRMED",
        staging_file_id="stage-1", resumable_offset=4194304,
    )
    public = public_finalize_telemetry(state)
    assert public is not None
    assert public["correlation_id"] == correlation
    assert public["candidate_object_id"] == "candidate-1"
    assert public["candidate_bytes"] == 36269883
    assert public["candidate_sha256"] == "a" * 64
    assert public["staging_file_id"] == "stage-1"
    assert public["previous_canonical_file_id"] == "old-1"
    assert public["resumable_offset"] == 4194304
    assert "SECRET-BEARER-CAPABILITY" not in json.dumps(public)
    assert "session_uri" not in json.dumps(public)
    again = ensure_finalize_telemetry(state, finalize)
    assert again["correlation_id"] == correlation


def test_finalize_snapshot_is_immutable_after_finalize_mutation():
    state = {"job_id": "job-1"}
    finalize = {"report_id": 42, "phase": "COMMIT", "annual_bytes": 10, "annual_sha256": "b" * 64}
    state["finalize"] = finalize
    record_finalize_event(state, finalize, "COMMIT_COMPLETE", canonical_file_id="drive-42")
    snap = finalize_telemetry_snapshot(state, finalize)
    finalize["telemetry"]["canonical_file_id"] = "changed-later"
    assert snap["canonical_file_id"] == "drive-42"
