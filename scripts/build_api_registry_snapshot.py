#!/usr/bin/env python3
"""Build the current WB/Ozon endpoint registry and compare it with baseline."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "api_registry"
CURRENT = REG / "current"
AUDIT = REG / "audit"
SOURCES = REG / "sources"

URLS = {
    "wb_index": "https://raw.githubusercontent.com/ZloyDeDD/wb-api-skill/main/swagger/_index.json",
    "wb_meta": "https://raw.githubusercontent.com/ZloyDeDD/wb-api-skill/main/swagger/_meta.json",
    "ozon_seller": "https://raw.githubusercontent.com/MissiaL/ozon-api/main/references/ozon-seller-openapi.json",
    "ozon_performance": "https://raw.githubusercontent.com/MissiaL/ozon-api/main/references/ozon-performance-openapi.json",
}
BASELINE = {
    "wildberries": REG / "catalog" / "wildberries.yaml",
    "ozon_seller": REG / "catalog" / "ozon_seller.yaml",
    "ozon_performance": REG / "catalog" / "ozon_performance.yaml",
}
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head")


def fetch_json(url: str) -> tuple[dict, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "marketplace-api-registry/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def prod_hosts(servers: list[dict] | None) -> list[str]:
    hosts: list[str] = []
    for server in servers or []:
        url = str(server.get("url") or "")
        host = url.removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        if host and "sandbox" not in host and host not in hosts:
            hosts.append(host)
    return hosts


def compact_ozon(spec: dict, marketplace: str) -> list[dict]:
    root_hosts = prod_hosts(spec.get("servers"))
    rows: list[dict] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        path_hosts = prod_hosts(path_item.get("servers")) or root_hosts
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            rows.append({
                "marketplace": marketplace,
                "method": method.upper(),
                "path": path,
                "hosts": prod_hosts(op.get("servers")) or path_hosts,
                "operation_id": op.get("operationId") or "",
                "tags": op.get("tags") or [],
                "deprecated": bool(op.get("deprecated", False)),
            })
    return sorted(rows, key=lambda r: (r["path"], r["method"]))


def compact_wb(index: dict) -> list[dict]:
    rows: list[dict] = []
    for item in index.get("endpoints") or []:
        rows.append({
            "marketplace": "wildberries",
            "method": str(item.get("method") or "").upper(),
            "path": item.get("path") or "",
            "hosts": [h for h in (item.get("hosts") or []) if "sandbox" not in h],
            "operation_id": item.get("operationId") or "",
            "section": item.get("section") or "",
            "category": item.get("category") or "",
            "tokens": item.get("tokens") or [],
            "readonly": bool(item.get("readonly", False)),
            "deprecated": bool(item.get("deprecated", False)),
            "source_file": item.get("file") or "",
        })
    return sorted(rows, key=lambda r: (r["path"], r["method"]))


def baseline_rows(path: Path, marketplace: str) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [{
        "marketplace": marketplace,
        "method": str(item.get("method") or "").upper(),
        "host": item.get("host") or doc.get("default_host") or "",
        "path": item.get("path") or "",
        "operation_id": item.get("operation_id") or "",
    } for item in (doc.get("endpoints") or [])]


def load_overrides() -> dict[tuple[str, str, str], dict]:
    doc = yaml.safe_load((REG / "status_overrides.yaml").read_text(encoding="utf-8")) or {}
    result = {}
    for item in doc.get("entries") or []:
        marketplace = item.get("marketplace") or ""
        if marketplace == "ozon":
            marketplace = "ozon_seller"
        key = (marketplace, str(item.get("method") or "").upper(), item.get("path") or "")
        result[key] = item
    return result


def enrich_status(rows: list[dict], overrides: dict) -> None:
    for row in rows:
        key = (row["marketplace"], row["method"], row["path"])
        override = overrides.get(key)
        if override:
            row["status"] = override.get("status") or "CURRENT"
            row["shutdown_date"] = override.get("shutdown_date") or ""
            row["replacement"] = override.get("replacement") or ""
            row["status_evidence"] = override.get("evidence") or []
        else:
            row["status"] = "DEPRECATED" if row.get("deprecated") else "CURRENT"
            row["shutdown_date"] = ""
            row["replacement"] = ""
            row["status_evidence"] = []


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "marketplace", "method", "path", "hosts", "operation_id", "status",
        "shutdown_date", "replacement", "deprecated", "section", "category", "tags",
        "tokens", "readonly", "source_file", "status_evidence",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            for field in ("hosts", "tags", "tokens", "status_evidence", "replacement"):
                if isinstance(out.get(field), (list, dict)):
                    out[field] = json.dumps(out[field], ensure_ascii=False, separators=(",", ":"))
            writer.writerow(out)


def main() -> None:
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    wb_index, wb_index_sha = fetch_json(URLS["wb_index"])
    wb_meta, wb_meta_sha = fetch_json(URLS["wb_meta"])
    ozon_seller, ozon_seller_sha = fetch_json(URLS["ozon_seller"])
    ozon_perf, ozon_perf_sha = fetch_json(URLS["ozon_performance"])

    current = {
        "wildberries": compact_wb(wb_index),
        "ozon_seller": compact_ozon(ozon_seller, "ozon_seller"),
        "ozon_performance": compact_ozon(ozon_perf, "ozon_performance"),
    }
    overrides = load_overrides()
    for rows in current.values():
        enrich_status(rows, overrides)

    baseline = {name: baseline_rows(path, name) for name, path in BASELINE.items()}
    for name, rows in current.items():
        write_json(CURRENT / f"{name}.json", rows)

    combined = [row for name in ("wildberries", "ozon_seller", "ozon_performance") for row in current[name]]
    write_json(REG / "api_catalog.json", combined)
    write_csv(REG / "api_catalog.csv", combined)

    diffs = {}
    for name in current:
        cur_keys = {(r["method"], r["path"]) for r in current[name]}
        base_keys = {(r["method"], r["path"]) for r in baseline[name]}
        diffs[name] = {
            "baseline_count": len(baseline[name]),
            "current_snapshot_count": len(current[name]),
            "current_status_counts": {
                status: sum(r.get("status") == status for r in current[name])
                for status in ("CURRENT", "DEPRECATED", "SHUTDOWN")
            },
            "current_not_in_baseline": [
                {"method": method, "path": path} for method, path in sorted(cur_keys - base_keys)
            ],
            "baseline_not_in_current": [
                {"method": method, "path": path} for method, path in sorted(base_keys - cur_keys)
            ],
        }

    status_counts = {
        status: sum(row.get("status") == status for row in combined)
        for status in ("CURRENT", "DEPRECATED", "SHUTDOWN")
    }
    summary = {
        "generated_at": fetched_at,
        "baseline_app_commit": "07a3408eab9a2abe4ba1e9dbcfdd8c67bed0458d",
        "baseline_total": sum(len(v) for v in baseline.values()),
        "current_snapshot_total": len(combined),
        "effective_status_counts": status_counts,
        "wildberries_official_snapshot_date": wb_meta.get("snapshot_date"),
        "diffs": diffs,
    }
    write_json(AUDIT / "summary.json", summary)

    provenance = {
        "generated_at": fetched_at,
        "policy": "public mirror used for enumeration; official notices/runtime remain authority for migrations",
        "sources": {
            "wildberries": {
                "official_origin": "dev.wildberries.ru OpenAPI/Swagger",
                "mirror": "ZloyDeDD/wb-api-skill",
                "mirror_snapshot_date": wb_meta.get("snapshot_date"),
                "index_url": URLS["wb_index"], "index_sha256": wb_index_sha,
                "meta_url": URLS["wb_meta"], "meta_sha256": wb_meta_sha,
            },
            "ozon_seller": {
                "official_origin": "https://docs.ozon.ru/api/seller/swagger.json",
                "mirror": "MissiaL/ozon-api", "url": URLS["ozon_seller"], "sha256": ozon_seller_sha,
            },
            "ozon_performance": {
                "official_origin": "https://docs.ozon.ru/api/performance/swagger.json",
                "mirror": "MissiaL/ozon-api", "url": URLS["ozon_performance"], "sha256": ozon_perf_sha,
            },
        },
    }
    write_json(SOURCES / "SOURCES.json", provenance)

    print(json.dumps({
        "baseline_total": summary["baseline_total"],
        "current_snapshot_total": summary["current_snapshot_total"],
        "effective_status_counts": status_counts,
        "counts": {name: len(rows) for name, rows in current.items()},
        "new_vs_baseline": {name: len(d["current_not_in_baseline"]) for name, d in diffs.items()},
        "baseline_not_in_current": {name: len(d["baseline_not_in_current"]) for name, d in diffs.items()},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
