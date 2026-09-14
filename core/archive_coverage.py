"""Dataset-generic coverage registry for canonical marketplace archives.

Unlike the WB weekly-finance report registry, advertising datasets are not
naturally keyed by provider reportId. Their completeness is proven by the set of
bounded provider requests that were planned and successfully committed.

A request key is a SHA-256 of canonical request identity (marketplace, cabinet,
dataset, provider operation, date interval and bounded scope). The registry is
written only after the corresponding canonical annual file commit succeeds.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

CSV_DELIMITER = ";"
REGISTRY_VERSION = "dataset_coverage_v1"
STATUS_COMPLETE = "COMPLETE"

REGISTRY_FIELDS = (
    "registry_version",
    "marketplace",
    "cabinet",
    "dataset",
    "operation_id",
    "request_key",
    "date_from",
    "date_to",
    "scope_kind",
    "scope_count",
    "scope_hash",
    "scope_json",
    "annual_file",
    "rows",
    "bytes",
    "sha256",
    "quality_status",
    "quality_flags",
    "status",
    "completed_at_utc",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_scope(scope: Mapping[str, Any] | None) -> dict[str, Any]:
    if not scope:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in sorted(scope.items()):
        if isinstance(value, (list, tuple, set)):
            try:
                items = sorted({int(item) for item in value})
            except (TypeError, ValueError):
                items = sorted({str(item) for item in value})
            normalized[str(key)] = items
        else:
            normalized[str(key)] = value
    return normalized


def request_identity(
    *,
    marketplace: str,
    cabinet: str,
    dataset: str,
    operation_id: str,
    date_from: str,
    date_to: str,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "marketplace": str(marketplace),
        "cabinet": str(cabinet),
        "dataset": str(dataset),
        "operation_id": str(operation_id),
        "date_from": str(date_from)[:10],
        "date_to": str(date_to)[:10],
        "scope": _normalized_scope(scope),
    }


def request_key(**identity_kwargs: Any) -> str:
    identity = request_identity(**identity_kwargs)
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _scope_meta(scope: Mapping[str, Any] | None) -> tuple[str, int, str, str]:
    normalized = _normalized_scope(scope)
    if not normalized:
        return "cabinet", 0, hashlib.sha256(b"{}").hexdigest(), "{}"
    if len(normalized) == 1:
        kind = next(iter(normalized))
        value = normalized[kind]
        count = len(value) if isinstance(value, list) else 1
    else:
        kind = "+".join(normalized)
        count = max(
            (len(value) if isinstance(value, list) else 1 for value in normalized.values()),
            default=0,
        )
    encoded = _canonical_json(normalized)
    return kind, count, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded


def coverage_record(
    *,
    marketplace: str,
    cabinet: str,
    dataset: str,
    operation_id: str,
    date_from: str,
    date_to: str,
    scope: Mapping[str, Any] | None,
    annual_file: str,
    rows: int,
    bytes_count: int,
    sha256: str,
    quality_status: str = "PASS",
    quality_flags: Iterable[str] = (),
    completed_at_utc: str | None = None,
) -> dict[str, Any]:
    identity = request_identity(
        marketplace=marketplace,
        cabinet=cabinet,
        dataset=dataset,
        operation_id=operation_id,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
    )
    kind, count, scope_hash, scope_json = _scope_meta(scope)
    return {
        "registry_version": REGISTRY_VERSION,
        **{key: identity[key] for key in (
            "marketplace", "cabinet", "dataset", "operation_id", "date_from", "date_to"
        )},
        "request_key": hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest(),
        "scope_kind": kind,
        "scope_count": count,
        "scope_hash": scope_hash,
        "scope_json": scope_json,
        "annual_file": str(annual_file),
        "rows": max(0, int(rows)),
        "bytes": max(0, int(bytes_count)),
        "sha256": str(sha256),
        "quality_status": str(quality_status),
        "quality_flags": _canonical_json(sorted({str(flag) for flag in quality_flags})),
        "status": STATUS_COMPLETE,
        "completed_at_utc": completed_at_utc or _utc_now(),
    }


def parse_registry(data: bytes | None) -> list[dict[str, str]]:
    if not data:
        return []
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=CSV_DELIMITER)
    return [dict(row) for row in reader]


def encode_registry(rows: Iterable[Mapping[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(REGISTRY_FIELDS),
        delimiter=CSV_DELIMITER,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: "" if row.get(field) is None else str(row.get(field)) for field in REGISTRY_FIELDS})
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def merge_coverage_registry(
    existing: bytes | None,
    records: Iterable[Mapping[str, Any]],
) -> bytes:
    by_key: dict[str, dict[str, Any]] = {}
    for row in parse_registry(existing):
        key = str(row.get("request_key") or "")
        if key:
            by_key[key] = dict(row)
    for record in records:
        key = str(record.get("request_key") or "")
        if not key:
            raise ValueError("coverage record has no request_key")
        if str(record.get("status")) != STATUS_COMPLETE:
            raise ValueError("only COMPLETE coverage records may be committed")
        by_key[key] = dict(record)
    rows = sorted(
        by_key.values(),
        key=lambda row: (
            str(row.get("marketplace") or ""),
            str(row.get("cabinet") or ""),
            str(row.get("dataset") or ""),
            str(row.get("date_from") or ""),
            str(row.get("date_to") or ""),
            str(row.get("request_key") or ""),
        ),
    )
    return encode_registry(rows)


def complete_request_keys(
    data: bytes | None,
    *,
    marketplace: str,
    cabinet: str,
    dataset: str,
) -> set[str]:
    return {
        str(row["request_key"])
        for row in parse_registry(data)
        if row.get("marketplace") == marketplace
        and row.get("cabinet") == cabinet
        and row.get("dataset") == dataset
        and row.get("status") == STATUS_COMPLETE
        and row.get("quality_status") in {"PASS", "PASS_WITH_FLAGS"}
        and row.get("request_key")
    }


def evaluate_coverage(
    *,
    expected_request_keys: Iterable[str],
    registry_data: bytes | None,
    marketplace: str,
    cabinet: str,
    dataset: str,
    canonical_file_present: bool,
) -> dict[str, Any]:
    expected = {str(key) for key in expected_request_keys if str(key)}
    complete = complete_request_keys(
        registry_data,
        marketplace=marketplace,
        cabinet=cabinet,
        dataset=dataset,
    )
    present = expected & complete
    missing = expected - complete
    if not canonical_file_present and expected:
        status = "ARCHIVE_FILE_MISSING"
    elif not expected:
        status = "NO_EXPECTED_REQUESTS"
    elif not present:
        status = "NO_COVERAGE"
    elif missing:
        status = "PARTIAL_COVERAGE"
    else:
        status = "FULL_COVERAGE"
    return {
        "status": status,
        "dataset": dataset,
        "expected_requests": len(expected),
        "complete_requests": len(present),
        "missing_requests": len(missing),
        "canonical_file_present": bool(canonical_file_present),
        "safe_for_archive_only_query": status == "FULL_COVERAGE",
    }
