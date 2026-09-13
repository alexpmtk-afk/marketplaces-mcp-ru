"""YDB backend for the selective canonical WB ORDERS historical store."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from .order_history import (
    OrderHistoryCoverage,
    canonical_json,
    normalize_wb_order_rows,
)


class QueryPool(Protocol):
    def execute_with_retries(
        self, query: str, parameters: dict[str, object] | None = None, **kwargs: object
    ): ...


@dataclass(slots=True)
class YdbRuntime:
    driver: Any
    pool: QueryPool

    @classmethod
    def connect(cls, connection_string: str, *, wait_timeout: float = 10.0) -> "YdbRuntime":
        if not connection_string.strip():
            raise ValueError("YDB connection string must be non-empty")
        ydb = _ydb()
        driver = ydb.Driver(
            connection_string=connection_string,
            credentials=ydb.iam.MetadataUrlCredentials(),
        )
        driver.wait(timeout=wait_timeout, fail_fast=True)
        return cls(driver=driver, pool=ydb.QuerySessionPool(driver))

    def close(self) -> None:
        close_pool = getattr(self.pool, "stop", None) or getattr(self.pool, "close", None)
        if callable(close_pool):
            close_pool()
        self.driver.stop(timeout=5)


class YdbOrderHistoryStore:
    """Durable multi-instance history of canonical WB Statistics Orders rows."""

    storage_scope = "ydb"

    def __init__(
        self,
        pool: QueryPool,
        *,
        runtime: YdbRuntime | None = None,
        table: str = "wb_order_history",
    ) -> None:
        self._pool = pool
        self._runtime = runtime
        self._table = _safe_table_name(table)
        self._coverage_table = _safe_table_name(table + "_coverage")
        self._init_schema()

    def _init_schema(self) -> None:
        ydb = _ydb()
        self._pool.execute_with_retries(
            f"""
            CREATE TABLE IF NOT EXISTS `{self._table}` (
                cabinet Utf8 NOT NULL,
                order_date Utf8 NOT NULL,
                srid Utf8 NOT NULL,
                last_change_date Utf8 NOT NULL,
                is_cancel Bool NOT NULL,
                finished_price Double NOT NULL,
                payload_json Utf8 NOT NULL,
                PRIMARY KEY (cabinet, order_date, srid)
            );
            """,
            retry_settings=ydb.RetrySettings(idempotent=True),
        )
        self._pool.execute_with_retries(
            f"""
            CREATE TABLE IF NOT EXISTS `{self._coverage_table}` (
                cabinet Utf8 NOT NULL,
                target_from Utf8 NOT NULL,
                covered_from Utf8 NOT NULL,
                covered_to Utf8 NOT NULL,
                watermark_last_change_date Utf8 NOT NULL,
                last_successful_sync_at Utf8 NOT NULL,
                status Utf8 NOT NULL,
                PRIMARY KEY (cabinet)
            );
            """,
            retry_settings=ydb.RetrySettings(idempotent=True),
        )

    def coverage(self, cabinet: str) -> OrderHistoryCoverage:
        ydb = _ydb()
        result = self._pool.execute_with_retries(
            f"""
            DECLARE $cabinet AS Utf8;
            SELECT target_from, covered_from, covered_to, watermark_last_change_date,
                   last_successful_sync_at, status
            FROM `{self._coverage_table}`
            WHERE cabinet = $cabinet
            LIMIT 1;
            """,
            {"$cabinet": _utf8(cabinet)},
            retry_settings=ydb.RetrySettings(idempotent=True),
        )
        row = _first_row(result)
        if row is None:
            return OrderHistoryCoverage(cabinet=cabinet)
        return OrderHistoryCoverage(
            cabinet=cabinet,
            target_from=_none_if_empty(_row_value(row, "target_from")),
            covered_from=_none_if_empty(_row_value(row, "covered_from")),
            covered_to=_none_if_empty(_row_value(row, "covered_to")),
            watermark_last_change_date=_none_if_empty(_row_value(row, "watermark_last_change_date")),
            last_successful_sync_at=_none_if_empty(_row_value(row, "last_successful_sync_at")),
            status=str(_row_value(row, "status") or "empty"),
        )

    def save_coverage(self, coverage: OrderHistoryCoverage) -> None:
        ydb = _ydb()
        query = f"""
        DECLARE $cabinet AS Utf8;
        DECLARE $target_from AS Utf8;
        DECLARE $covered_from AS Utf8;
        DECLARE $covered_to AS Utf8;
        DECLARE $watermark AS Utf8;
        DECLARE $last_sync AS Utf8;
        DECLARE $status AS Utf8;
        UPSERT INTO `{self._coverage_table}`
            (cabinet, target_from, covered_from, covered_to,
             watermark_last_change_date, last_successful_sync_at, status)
        VALUES
            ($cabinet, $target_from, $covered_from, $covered_to,
             $watermark, $last_sync, $status);
        """
        self._pool.execute_with_retries(
            query,
            {
                "$cabinet": _utf8(coverage.cabinet),
                "$target_from": _utf8(coverage.target_from or ""),
                "$covered_from": _utf8(coverage.covered_from or ""),
                "$covered_to": _utf8(coverage.covered_to or ""),
                "$watermark": _utf8(coverage.watermark_last_change_date or ""),
                "$last_sync": _utf8(coverage.last_successful_sync_at or ""),
                "$status": _utf8(coverage.status),
            },
            retry_settings=ydb.RetrySettings(idempotent=True),
        )

    def upsert_rows(self, cabinet: str, rows: list[dict[str, Any]]) -> int:
        normalized = normalize_wb_order_rows(rows)
        if not normalized:
            return 0
        ydb = _ydb()
        row_type = (
            ydb.StructType()
            .add_member("cabinet", ydb.PrimitiveType.Utf8)
            .add_member("order_date", ydb.PrimitiveType.Utf8)
            .add_member("srid", ydb.PrimitiveType.Utf8)
            .add_member("last_change_date", ydb.PrimitiveType.Utf8)
            .add_member("is_cancel", ydb.PrimitiveType.Bool)
            .add_member("finished_price", ydb.PrimitiveType.Double)
            .add_member("payload_json", ydb.PrimitiveType.Utf8)
        )
        values = []
        for row in normalized:
            values.append({
                "cabinet": cabinet,
                "order_date": str(row["date"])[:10],
                "srid": str(row["srid"]),
                "last_change_date": str(row["lastChangeDate"]),
                "is_cancel": bool(row.get("isCancel", False)),
                "finished_price": float(row["finishedPrice"]),
                "payload_json": canonical_json(row),
            })
        self._pool.execute_with_retries(
            f"""
            DECLARE $rows AS List<Struct<
                cabinet:Utf8,order_date:Utf8,srid:Utf8,last_change_date:Utf8,
                is_cancel:Bool,finished_price:Double,payload_json:Utf8
            >>;
            UPSERT INTO `{self._table}`
            SELECT cabinet, order_date, srid, last_change_date, is_cancel,
                   finished_price, payload_json
            FROM AS_TABLE($rows);
            """,
            {"$rows": (values, ydb.ListType(row_type))},
            retry_settings=ydb.RetrySettings(idempotent=True),
        )
        return len(values)

    def read_rows(self, cabinet: str, start: date, end: date) -> list[dict[str, Any]]:
        ydb = _ydb()
        result = self._pool.execute_with_retries(
            f"""
            DECLARE $cabinet AS Utf8;
            DECLARE $from_date AS Utf8;
            DECLARE $to_date AS Utf8;
            SELECT payload_json
            FROM `{self._table}`
            WHERE cabinet = $cabinet
              AND order_date >= $from_date
              AND order_date <= $to_date
            ORDER BY order_date, srid;
            """,
            {
                "$cabinet": _utf8(cabinet),
                "$from_date": _utf8(start.isoformat()),
                "$to_date": _utf8(end.isoformat()),
            },
            retry_settings=ydb.RetrySettings(idempotent=True),
        )
        return [json.loads(str(_row_value(row, "payload_json"))) for row in _rows(result)]

    def count(self, cabinet: str) -> int:
        ydb = _ydb()
        result = self._pool.execute_with_retries(
            f"""
            DECLARE $cabinet AS Utf8;
            SELECT COUNT(*) AS cnt FROM `{self._table}` WHERE cabinet = $cabinet;
            """,
            {"$cabinet": _utf8(cabinet)},
            retry_settings=ydb.RetrySettings(idempotent=True),
        )
        row = _first_row(result)
        return int(_row_value(row, "cnt") or 0) if row is not None else 0

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()


def build_order_history_store_from_env() -> YdbOrderHistoryStore | None:
    connection_string = os.environ.get("MARKETPLACE_MCP_YDB_ENDPOINT", "").strip()
    if not connection_string:
        return None
    runtime = YdbRuntime.connect(connection_string)
    return YdbOrderHistoryStore(runtime.pool, runtime=runtime)


def _ydb():
    try:
        import ydb
        import ydb.iam
    except ImportError as exc:
        raise RuntimeError(
            "YDB history backend is configured but ydb[yc] is not installed"
        ) from exc
    return ydb


def _utf8(value: str):
    ydb = _ydb()
    return (value, ydb.PrimitiveType.Utf8)


def _rows(result: object) -> list[object]:
    if result is None:
        return []
    result_sets = list(result) if not isinstance(result, list) else result
    rows: list[object] = []
    for result_set in result_sets:
        rows.extend(list(getattr(result_set, "rows", []) or []))
    return rows


def _first_row(result: object) -> object | None:
    rows = _rows(result)
    return rows[0] if rows else None


def _row_value(row: object, field: str) -> object:
    if isinstance(row, dict):
        return row[field]
    try:
        return row[field]  # type: ignore[index]
    except (TypeError, KeyError):
        return getattr(row, field)


def _none_if_empty(value: object) -> str | None:
    text = str(value or "")
    return text or None


def _safe_table_name(value: str) -> str:
    if not value or any(not (char.isalnum() or char == "_") for char in value):
        raise ValueError("YDB table name must contain only letters, digits and underscore")
    return value
