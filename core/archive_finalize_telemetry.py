"""Durable, non-secret telemetry for archive finalize transactions."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

MAX_EVENTS = 40
_PUBLIC_KEYS = {
    "candidate_object_id",
    "candidate_bytes",
    "candidate_sha256",
    "staging_file_id",
    "canonical_file_id",
    "previous_canonical_file_id",
    "resumable_offset",
    "transition",
    "retry_count",
    "canonical_state",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _correlation_id(job_id: str, report_id: int) -> str:
    raw = f"{job_id}:{int(report_id)}".encode("utf-8")
    return "archive-finalize-" + hashlib.sha256(raw).hexdigest()[:20]


def ensure_finalize_telemetry(
    state: dict[str, Any],
    finalize: dict[str, Any],
    *,
    candidate_object_id: str | None = None,
) -> dict[str, Any]:
    """Attach/recover stable telemetry for a finalize transaction.

    This is intentionally safe for already-running jobs created before telemetry
    existed. Correlation identity is deterministic by job/report and no bearer-like
    resumable session URI is ever copied here.
    """
    job_id = str(state.get("job_id") or "")
    report_id = int(finalize.get("report_id") or 0)
    telemetry = dict(finalize.get("telemetry") or {})
    telemetry.setdefault("correlation_id", _correlation_id(job_id, report_id))
    telemetry["job_id"] = job_id
    telemetry["report_id"] = report_id
    telemetry["phase"] = str(finalize.get("phase") or "")
    telemetry["candidate_bytes"] = int(finalize.get("annual_bytes", 0) or 0)
    telemetry["candidate_sha256"] = str(finalize.get("annual_sha256") or "")
    if candidate_object_id:
        telemetry["candidate_object_id"] = str(candidate_object_id)
    telemetry.setdefault("events", [])
    finalize["telemetry"] = telemetry
    state["finalize"] = finalize
    return telemetry


def sync_resumable_telemetry(
    state: dict[str, Any],
    finalize: dict[str, Any],
    *,
    candidate_object_id: str | None = None,
) -> dict[str, Any]:
    telemetry = ensure_finalize_telemetry(
        state, finalize, candidate_object_id=candidate_object_id
    )
    upload = dict(finalize.get("resumable_upload") or {})
    mapping = {
        "target_file_id": "staging_file_id",
        "staged_file_id": "staging_file_id",
        "previous_canonical_file_id": "previous_canonical_file_id",
    }
    for source, target in mapping.items():
        value = str(upload.get(source) or "").strip()
        if value:
            telemetry[target] = value
    telemetry["resumable_offset"] = int(upload.get("offset", 0) or 0)
    telemetry["phase"] = str(finalize.get("phase") or "")
    finalize["telemetry"] = telemetry
    state["finalize"] = finalize
    return telemetry


def record_finalize_event(
    state: dict[str, Any],
    finalize: dict[str, Any],
    event: str,
    **fields: Any,
) -> dict[str, Any]:
    telemetry = ensure_finalize_telemetry(state, finalize)
    safe_fields = {key: value for key, value in fields.items() if key in _PUBLIC_KEYS}
    for key, value in safe_fields.items():
        if value is not None and value != "":
            telemetry[key] = value
    telemetry["phase"] = str(finalize.get("phase") or "")
    item = {
        "at_utc": _utc_now(),
        "event": str(event),
        "phase": telemetry["phase"],
        **safe_fields,
    }
    events = list(telemetry.get("events") or [])
    events.append(item)
    telemetry["events"] = events[-MAX_EVENTS:]
    telemetry["last_event"] = str(event)
    telemetry["updated_at_utc"] = item["at_utc"]
    finalize["telemetry"] = telemetry
    state["finalize"] = finalize
    return telemetry


def finalize_telemetry_snapshot(
    state: dict[str, Any], finalize: dict[str, Any]
) -> dict[str, Any]:
    telemetry = ensure_finalize_telemetry(state, finalize)
    # Defensive copy: later state mutations must not rewrite historical evidence.
    return deepcopy(telemetry)


def public_finalize_telemetry(state: dict[str, Any]) -> dict[str, Any] | None:
    finalize = dict(state.get("finalize") or {})
    telemetry = dict(finalize.get("telemetry") or {})
    if not telemetry:
        telemetry = dict(state.get("last_finalize_telemetry") or {})
    if not telemetry:
        return None
    allowed = {
        "correlation_id",
        "job_id",
        "report_id",
        "phase",
        "candidate_object_id",
        "candidate_bytes",
        "candidate_sha256",
        "staging_file_id",
        "canonical_file_id",
        "previous_canonical_file_id",
        "resumable_offset",
        "transition",
        "retry_count",
        "canonical_state",
        "last_event",
        "updated_at_utc",
        "events",
    }
    # `session_uri` is intentionally impossible to expose through this projection.
    return {key: deepcopy(value) for key, value in telemetry.items() if key in allowed}
