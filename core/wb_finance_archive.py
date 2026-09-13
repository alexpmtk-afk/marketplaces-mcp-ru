"""Durable Wildberries weekly finance archive.

Canonical archive policy:
- only weekly ``reportType=1`` ("Основной") belongs to the main annual base;
- one logical week may be split into multiple provider report IDs at month/year
  boundaries; every fragment is stored, while the registry maps it back to the
  Monday-Sunday logical week;
- one CSV per WB cabinet and calendar year, updated idempotently;
- provider rows are preserved verbatim (all fields), with no business formulas;
- report registry is authoritative for discovery state, while the annual CSV
  also deduplicates on (reportId, rrdId) as a second safety net.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .archive_drive import GoogleDriveArchiveStore
from .business_registry import resolve_business_cabinet
from .rate_limit import redis_connection_kwargs, redis_url_from_env
from .tools import resolve_named_cabinet

MAIN_REPORT_TYPE = 1
DATASET = "wb_weekly_finance_main"
CSV_DELIMITER = ";"
ARCHIVE_CABINETS = ("wb_dmitrieva", "wb_novokshenov", "wb_laser_master")
REGISTRY_FIELDS = (
    "marketplace",
    "cabinet",
    "dataset",
    "report_type",
    "report_id",
    "date_from",
    "date_to",
    "logical_week_from",
    "logical_week_to",
    "create_date",
    "year",
    "annual_file",
    "drive_file_id",
    "rows",
    "bytes",
    "sha256",
    "status",
    "ingested_at_utc",
)


@dataclass(frozen=True)
class ReportFragment:
    report_id: int
    date_from: str
    date_to: str
    create_date: str
    report_type: int
    logical_week_from: str
    logical_week_to: str

    @property
    def year(self) -> int:
        return date.fromisoformat(self.logical_week_to).year


def logical_week(period_date: str) -> tuple[str, str]:
    """Map any fragment date to its Monday-Sunday logical report week."""
    value = date.fromisoformat(str(period_date)[:10])
    start = value - timedelta(days=value.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def parse_csv_bytes(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    if not data:
        return [], []
    text = data.decode("utf-8-sig")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = CSV_DELIMITER
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fieldnames = list(reader.fieldnames or [])
    return fieldnames, [dict(row) for row in reader]


def encode_csv(fieldnames: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    fields = list(fieldnames)
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        delimiter=CSV_DELIMITER,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _stringify(row.get(field)) for field in fields})
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def merge_annual_csv(existing: bytes | None, new_rows: list[dict[str, Any]]) -> tuple[bytes, dict[str, Any]]:
    """Merge provider rows into one deterministic annual CSV without duplicates."""
    old_fields, old_rows = parse_csv_bytes(existing or b"")
    fieldnames = list(old_fields)
    seen_fields = set(fieldnames)
    for row in new_rows:
        for key in row.keys():
            if key not in seen_fields:
                fieldnames.append(key)
                seen_fields.add(key)
    if not fieldnames and new_rows:
        fieldnames = list(new_rows[0].keys())
    if not fieldnames:
        return existing or b"", {"added_rows": 0, "total_rows": len(old_rows), "columns": 0}

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    anonymous_markers: set[str] = set()

    def add_row(row: dict[str, Any], *, replace: bool) -> bool:
        report_id = _stringify(row.get("reportId"))
        rrd_id = _stringify(row.get("rrdId"))
        if report_id and rrd_id:
            key = (report_id, rrd_id)
            existed = key in by_key
            if replace or not existed:
                by_key[key] = row
            return not existed
        marker = json.dumps(
            {k: _stringify(row.get(k)) for k in fieldnames},
            ensure_ascii=False,
            sort_keys=True,
        )
        if marker in anonymous_markers:
            return False
        anonymous_markers.add(marker)
        anonymous.append(row)
        return True

    for row in old_rows:
        add_row(row, replace=False)
    before = len(by_key) + len(anonymous)
    for row in new_rows:
        add_row(row, replace=False)

    rows: list[dict[str, Any]] = list(by_key.values()) + anonymous
    rows.sort(
        key=lambda row: (
            _stringify(row.get("dateFrom")),
            _stringify(row.get("dateTo")),
            int(_stringify(row.get("reportId")) or 0),
            int(_stringify(row.get("rrdId")) or 0),
        )
    )
    payload = encode_csv(fieldnames, rows)
    return payload, {
        "added_rows": len(rows) - before,
        "total_rows": len(rows),
        "columns": len(fieldnames),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def merge_registry(existing: bytes | None, records: list[dict[str, Any]]) -> bytes:
    _, old_rows = parse_csv_bytes(existing or b"")
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in old_rows:
        key = (
            _stringify(row.get("cabinet")),
            _stringify(row.get("dataset")),
            _stringify(row.get("report_id")),
        )
        if all(key):
            by_key[key] = row
    for row in records:
        key = (
            _stringify(row.get("cabinet")),
            _stringify(row.get("dataset")),
            _stringify(row.get("report_id")),
        )
        if all(key):
            by_key[key] = row
    rows = sorted(
        by_key.values(),
        key=lambda row: (
            _stringify(row.get("marketplace")),
            _stringify(row.get("cabinet")),
            _stringify(row.get("logical_week_from")),
            int(_stringify(row.get("report_id")) or 0),
        ),
    )
    return encode_csv(REGISTRY_FIELDS, rows)


def registry_complete_ids(data: bytes | None, cabinet: str) -> set[int]:
    _, rows = parse_csv_bytes(data or b"")
    result: set[int] = set()
    for row in rows:
        if (
            row.get("cabinet") != cabinet
            or row.get("dataset") != DATASET
            or row.get("status") != "COMPLETE"
        ):
            continue
        try:
            result.add(int(row.get("report_id") or 0))
        except ValueError:
            pass
    return result


def normalize_main_fragments(rows: Iterable[dict[str, Any]]) -> list[ReportFragment]:
    fragments: list[ReportFragment] = []
    for row in rows:
        try:
            report_type = int(row.get("reportType") or 0)
            report_id = int(row.get("reportId") or 0)
        except (TypeError, ValueError):
            continue
        if report_type != MAIN_REPORT_TYPE or report_id <= 0:
            continue
        date_from = str(row.get("dateFrom") or "")[:10]
        date_to = str(row.get("dateTo") or "")[:10]
        if not date_from or not date_to:
            continue
        week_from, week_to = logical_week(date_from)
        fragments.append(
            ReportFragment(
                report_id=report_id,
                date_from=date_from,
                date_to=date_to,
                create_date=str(row.get("createDate") or "")[:10],
                report_type=report_type,
                logical_week_from=week_from,
                logical_week_to=week_to,
            )
        )
    fragments.sort(key=lambda item: (item.logical_week_from, item.date_from, item.report_id))
    return fragments


class ArchiveLock:
    """Redis-backed lock so two MCP clients cannot update the same archive at once."""

    RELEASE_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, key: str = "marketplace-archive:v1:wb-finance", ttl_seconds: int = 900) -> None:
        self.key = key
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.token = secrets.token_hex(16)
        self.client = None

    async def __aenter__(self) -> "ArchiveLock":
        url = redis_url_from_env()
        if not url:
            raise RuntimeError("Shared Redis is required for archive updates")
        import redis.asyncio as redis_async

        self.client = redis_async.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
            **redis_connection_kwargs(url),
        )
        acquired = await self.client.set(self.key, self.token, nx=True, ex=self.ttl_seconds)
        if not acquired:
            raise RuntimeError("Archive update is already running")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.client is not None:
            try:
                await self.client.eval(self.RELEASE_SCRIPT, 1, self.key, self.token)
            finally:
                await self.client.aclose()


class WBFinanceArchiveManager:
    def __init__(self, wb_module: Any, store: GoogleDriveArchiveStore) -> None:
        self.wb = wb_module
        self.store = store

    @staticmethod
    def _retry_after(data: dict[str, Any]) -> float:
        try:
            return float(data.get("retry_after_seconds", 0) or data.get("retry_after_sec", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    async def _call_wait(self, spec: Any, *, creds: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        while True:
            data = await self.wb.client.call_spec(spec, creds_override=creds, **kwargs)
            retry_after = self._retry_after(data)
            if retry_after > 0 and (
                data.get("error_type") == "rate_limit"
                or data.get("error") in {"rate_limit", "rate_limit_busy"}
            ):
                await asyncio.sleep(min(90.0, max(1.0, retry_after + 0.5)))
                continue
            return data

    def _resolve_creds(self, cabinet: str) -> dict[str, str]:
        business = resolve_business_cabinet("wb", cabinet)
        credential_name = business.cabinet if business else cabinet
        creds, error = resolve_named_cabinet(self.wb.client, credential_name)
        if error:
            raise RuntimeError(str(error.get("message") or error))
        assert creds is not None
        return creds

    async def _list_main_fragments(self, cabinet: str, year: int) -> list[ReportFragment]:
        creds = self._resolve_creds(cabinet)
        spec = self.wb.catalog.get("wb_finance_sales_reports_list")
        if spec is None:
            raise RuntimeError("wb_finance_sales_reports_list contract is missing")
        start = date(year, 1, 1)
        end = min(date(year, 12, 31), date.today())
        offset = 0
        rows: list[dict[str, Any]] = []
        while True:
            result = await self._call_wait(
                spec,
                creds=creds,
                json_body={
                    "dateFrom": start.isoformat(),
                    "dateTo": end.isoformat(),
                    "period": "weekly",
                    "limit": 1000,
                    "offset": offset,
                },
            )
            if not result.get("ok"):
                raise RuntimeError(f"WB report discovery failed for {cabinet}: {result}")
            page = result.get("data") or []
            if not isinstance(page, list):
                raise RuntimeError(f"WB report discovery returned non-list data for {cabinet}")
            rows.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
        return [fragment for fragment in normalize_main_fragments(rows) if fragment.year == year]

    async def _download_fragment(self, cabinet: str, report_id: int) -> list[dict[str, Any]]:
        creds = self._resolve_creds(cabinet)
        spec = self.wb.catalog.get("wb_finance_sales_reports_detailed_by_id")
        if spec is None:
            raise RuntimeError("wb_finance_sales_reports_detailed_by_id contract is missing")
        rows: list[dict[str, Any]] = []
        rrd_id = 0
        seen_rrd: set[int] = set()
        while True:
            result = await self._call_wait(
                spec,
                creds=creds,
                path_values={"reportId": int(report_id)},
                json_body={"limit": 100000, "rrdId": int(rrd_id)},
            )
            status = int(result.get("status", 0) or 0)
            if result.get("ok") is True and status == 204:
                break
            if result.get("ok") is not True or not (200 <= status < 300):
                raise RuntimeError(f"WB report {report_id} download failed: {result}")
            page = result.get("data") or []
            if not isinstance(page, list):
                raise RuntimeError(f"WB report {report_id} detail returned non-list data")
            if not page:
                break
            for row in page:
                if int(row.get("reportId") or report_id) != report_id:
                    raise RuntimeError(f"WB report {report_id} returned a foreign reportId")
                if int(row.get("reportType") or 0) != MAIN_REPORT_TYPE:
                    raise RuntimeError(f"WB report {report_id} is not reportType=1")
            rows.extend(page)
            try:
                next_rrd = int(page[-1]["rrdId"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"WB report {report_id} has no terminal rrdId") from exc
            if next_rrd <= rrd_id or next_rrd in seen_rrd:
                raise RuntimeError(f"WB report {report_id} rrdId did not advance")
            seen_rrd.add(next_rrd)
            rrd_id = next_rrd
        return rows

    async def _registry_location(self) -> tuple[str, str]:
        folder = await self.store.ensure_folder_path(["app", "registry"])
        return folder, "reports_registry.csv"

    async def _annual_location(self, cabinet: str, year: int) -> tuple[str, str]:
        folder = await self.store.ensure_folder_path(
            ["База данных", "WB", cabinet, str(year), "finance", "weekly", "main"]
        )
        return folder, f"{cabinet}__weekly_main__{year}.csv"

    async def status(self, year: int) -> dict[str, Any]:
        registry_folder, registry_name = await self._registry_location()
        _, registry_data = await self.store.download_named(registry_folder, registry_name)
        result: dict[str, Any] = {
            "ok": True,
            "marketplace": "wb",
            "dataset": DATASET,
            "year": year,
            "storage": await self.store.status(),
            "cabinets": {},
        }
        for cabinet in ARCHIVE_CABINETS:
            annual_folder, annual_name = await self._annual_location(cabinet, year)
            annual_file = await self.store.find_child(annual_folder, annual_name)
            complete_ids = registry_complete_ids(registry_data, cabinet)
            result["cabinets"][cabinet] = {
                "complete_report_ids": len(complete_ids),
                "annual_file": annual_name,
                "drive_file_id": annual_file.id if annual_file else None,
                "bytes": annual_file.size if annual_file else 0,
            }
        return result

    async def update(
        self,
        *,
        year: int,
        cabinets: Iterable[str] | None = None,
        max_reports_per_cabinet: int = 4,
    ) -> dict[str, Any]:
        selected = tuple(cabinets or ARCHIVE_CABINETS)
        invalid = [cabinet for cabinet in selected if cabinet not in ARCHIVE_CABINETS]
        if invalid:
            raise ValueError(f"Unsupported WB archive cabinets: {invalid}")
        if year < 2025 or year > date.today().year:
            raise ValueError("year must be between 2025 and the current year")
        max_reports_per_cabinet = max(1, min(20, int(max_reports_per_cabinet)))

        async with ArchiveLock():
            registry_folder, registry_name = await self._registry_location()
            _, registry_data = await self.store.download_named(registry_folder, registry_name)
            records: list[dict[str, Any]] = []

            async def update_one(cabinet: str) -> dict[str, Any]:
                fragments = await self._list_main_fragments(cabinet, year)
                complete = registry_complete_ids(registry_data, cabinet)
                pending = [fragment for fragment in fragments if fragment.report_id not in complete]
                todo = pending[:max_reports_per_cabinet]
                annual_folder, annual_name = await self._annual_location(cabinet, year)
                annual_file, annual_data = await self.store.download_named(annual_folder, annual_name)
                new_rows: list[dict[str, Any]] = []
                fragment_rows: dict[int, int] = {}
                for fragment in todo:
                    rows = await self._download_fragment(cabinet, fragment.report_id)
                    fragment_rows[fragment.report_id] = len(rows)
                    new_rows.extend(rows)
                if todo:
                    merged, stats = merge_annual_csv(annual_data, new_rows)
                    annual_file = await self.store.upload_bytes(
                        annual_folder, annual_name, merged, mime_type="text/csv"
                    )
                    annual_data = merged
                    for fragment in todo:
                        records.append(
                            {
                                "marketplace": "wb",
                                "cabinet": cabinet,
                                "dataset": DATASET,
                                "report_type": MAIN_REPORT_TYPE,
                                "report_id": fragment.report_id,
                                "date_from": fragment.date_from,
                                "date_to": fragment.date_to,
                                "logical_week_from": fragment.logical_week_from,
                                "logical_week_to": fragment.logical_week_to,
                                "create_date": fragment.create_date,
                                "year": year,
                                "annual_file": annual_name,
                                "drive_file_id": annual_file.id,
                                "rows": fragment_rows.get(fragment.report_id, 0),
                                "bytes": len(merged),
                                "sha256": hashlib.sha256(merged).hexdigest(),
                                "status": "COMPLETE",
                                "ingested_at_utc": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                else:
                    fields, current_rows = parse_csv_bytes(annual_data or b"")
                    stats = {
                        "added_rows": 0,
                        "total_rows": len(current_rows),
                        "columns": len(fields),
                        "bytes": len(annual_data or b""),
                        "sha256": (
                            hashlib.sha256(annual_data).hexdigest() if annual_data else None
                        ),
                    }
                return {
                    "cabinet": cabinet,
                    "discovered_main_reports": len(fragments),
                    "previously_complete": len(complete),
                    "downloaded_report_ids": [fragment.report_id for fragment in todo],
                    "remaining_report_ids": [fragment.report_id for fragment in pending[len(todo):]],
                    "annual_file": annual_name,
                    "drive_file_id": annual_file.id if annual_file else None,
                    **stats,
                }

            cabinet_results = await asyncio.gather(*(update_one(cabinet) for cabinet in selected))
            if records:
                registry_payload = merge_registry(registry_data, records)
                await self.store.upload_bytes(
                    registry_folder, registry_name, registry_payload, mime_type="text/csv"
                )
            more_pending = any(item["remaining_report_ids"] for item in cabinet_results)
            return {
                "ok": True,
                "marketplace": "wb",
                "dataset": DATASET,
                "year": year,
                "complete": not more_pending,
                "continue_required": more_pending,
                "cabinets": cabinet_results,
                "instruction": (
                    "Call marketplace_archive_update again with the same year until complete=true."
                    if more_pending
                    else "Archive is current for all selected WB cabinets."
                ),
            }
