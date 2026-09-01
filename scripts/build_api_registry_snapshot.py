#!/usr/bin/env python3
"""Build a current, normalized WB/Ozon API registry and baseline drift report.

The registry is control data only. Runtime MCP catalogs are not modified here.
Current endpoint enumeration/contracts come from reproducible public snapshots
of the marketplaces' official OpenAPI documents; lifecycle overrides require
separate official-change evidence.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "api_registry"
CURRENT = REG / "current"
AUDIT = REG / "audit"
SOURCES = REG / "sources"

WB_REPO = "https://raw.githubusercontent.com/ZloyDeDD/wb-api-skill/main/swagger"
URLS = {
    "wb_index": f"{WB_REPO}/_index.json",
    "wb_meta": f"{WB_REPO}/_meta.json",
    "ozon_seller": "https://raw.githubusercontent.com/MissiaL/ozon-api/main/references/ozon-seller-openapi.json",
    "ozon_performance": "https://raw.githubusercontent.com/MissiaL/ozon-api/main/references/ozon-performance-openapi.json",
}
BASELINE = {
    "wildberries": REG / "catalog" / "wildberries.yaml",
    "ozon_seller": REG / "catalog" / "ozon_seller.yaml",
    "ozon_performance": REG / "catalog" / "ozon_performance.yaml",
}
DEFAULT_HOSTS = {
    "ozon_seller": "api-seller.ozon.ru",
    "ozon_performance": "api-performance.ozon.ru",
}
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head")


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "marketplace-api-registry/1.1"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def fetch_json(url: str) -> tuple[dict, str]:
    raw = fetch_bytes(url)
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def fetch_yaml(url: str) -> tuple[dict, str]:
    raw = fetch_bytes(url)
    return yaml.safe_load(raw.decode("utf-8")) or {}, hashlib.sha256(raw).hexdigest()


def prod_hosts(servers: list[dict] | None) -> list[str]:
    hosts: list[str] = []
    for server in servers or []:
        url = str(server.get("url") or "")
        host = url.removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        if host and "sandbox" not in host and host not in hosts:
            hosts.append(host)
    return hosts


def resolve_local_ref(spec: dict, value: Any) -> Any:
    if not isinstance(value, dict) or "$ref" not in value:
        return value
    ref = str(value["$ref"])
    if not ref.startswith("#/"):
        return value
    current: Any = spec
    try:
        for part in ref[2:].split("/"):
            current = current[part.replace("~1", "/").replace("~0", "~")]
        return current
    except (KeyError, TypeError):
        return value


def schema_shape(schema: Any, depth: int = 0) -> Any:
    """Compact OpenAPI schema without copying large prose/examples."""
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        return {"$ref": schema["$ref"]}
    out = {k: schema[k] for k in ("type", "format", "nullable", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems") if k in schema}
    if "enum" in schema:
        out["enum"] = schema["enum"]
    if "required" in schema:
        out["required"] = schema["required"]
    if depth < 2 and isinstance(schema.get("items"), dict):
        out["items"] = schema_shape(schema["items"], depth + 1)
    if depth < 1 and isinstance(schema.get("properties"), dict):
        out["properties"] = {name: schema_shape(value, depth + 1) for name, value in schema["properties"].items()}
    for combiner in ("oneOf", "anyOf", "allOf"):
        if combiner in schema and depth < 1:
            out[combiner] = [schema_shape(x, depth + 1) for x in schema[combiner]]
    return out


def normalize_parameters(spec: dict, path_item: dict, op: dict) -> list[dict]:
    rows = []
    for raw in list(path_item.get("parameters") or []) + list(op.get("parameters") or []):
        resolved = resolve_local_ref(spec, raw)
        if not isinstance(resolved, dict):
            continue
        rows.append({
            "name": resolved.get("name") or str(raw.get("$ref", "")).rsplit("/", 1)[-1],
            "in": resolved.get("in") or "",
            "required": bool(resolved.get("required", False)),
            "schema": schema_shape(resolved.get("schema") or {}),
        })
    return rows


def normalize_request_body(spec: dict, op: dict) -> dict | None:
    raw = op.get("requestBody")
    if not raw:
        return None
    resolved = resolve_local_ref(spec, raw)
    if not isinstance(resolved, dict):
        return {"$ref": raw.get("$ref")} if isinstance(raw, dict) else None
    content = {}
    for mime, item in (resolved.get("content") or {}).items():
        if isinstance(item, dict):
            content[mime] = schema_shape(item.get("schema") or {})
    result = {"required": bool(resolved.get("required", False)), "content": content}
    if isinstance(raw, dict) and "$ref" in raw:
        result["source_ref"] = raw["$ref"]
    return result


def normalize_responses(spec: dict, op: dict) -> dict:
    out = {}
    for status, raw in (op.get("responses") or {}).items():
        resolved = resolve_local_ref(spec, raw)
        if not isinstance(resolved, dict):
            continue
        content = {}
        for mime, item in (resolved.get("content") or {}).items():
            if isinstance(item, dict):
                content[mime] = schema_shape(item.get("schema") or {})
        row = {"content": content}
        if isinstance(raw, dict) and "$ref" in raw:
            row["source_ref"] = raw["$ref"]
        out[str(status)] = row
    return out


def infer_pagination(parameters: list[dict]) -> dict:
    names = {str(p.get("name", "")).lower() for p in parameters}
    for name, mode in (("cursor", "cursor"), ("last_id", "last_id"), ("offset", "offset"), ("page", "page"), ("next", "next"), ("rrdid", "rrdid")):
        if name in names:
            return {"mode": mode, "confidence": "parameter_detected"}
    return {"mode": "none_or_response_driven", "confidence": "not_inferred"}


def baseline_inventory(path: Path, marketplace: str) -> tuple[list[dict], dict[tuple[str, str], dict]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = []
    index = {}
    for item in doc.get("endpoints") or []:
        row = {
            "marketplace": marketplace,
            "method": str(item.get("method") or "").upper(),
            "host": item.get("host") or doc.get("default_host") or "",
            "path": item.get("path") or "",
            "operation_id": item.get("operation_id") or "",
            "section": item.get("section") or "",
            "scope": item.get("scope") or "",
            "safety": item.get("safety") or "UNVERIFIED",
            "summary": item.get("summary") or "",
            "params": item.get("params") or {},
            "pagination": item.get("pagination") or "none",
            "items_path": item.get("items_path") or "",
            "rate_limit": item.get("rate_limit") or "",
            "keywords": item.get("keywords") or [],
            "documentation": item.get("doc") or "",
        }
        rows.append(row)
        index[(row["method"], row["path"])] = row
    return rows, index


def operation_record(*, spec: dict, path: str, path_item: dict, method: str, op: dict, marketplace: str, source_file: str, baseline: dict | None) -> dict:
    parameters = normalize_parameters(spec, path_item, op)
    hosts = prod_hosts(op.get("servers")) or prod_hosts(path_item.get("servers")) or prod_hosts(spec.get("servers"))
    if not hosts and DEFAULT_HOSTS.get(marketplace):
        hosts = [DEFAULT_HOSTS[marketplace]]
    deprecated = bool(op.get("deprecated", False))
    readonly = op.get("x-readonly-method")
    if baseline:
        safety = baseline["safety"]
    elif marketplace == "wildberries" and readonly is True:
        safety = "read"
    elif marketplace == "wildberries" and method.upper() == "DELETE":
        safety = "destructive"
    elif marketplace == "wildberries":
        safety = "write"
    else:
        safety = "UNVERIFIED"
    return {
        "marketplace": marketplace,
        "operation_id": op.get("operationId") or (baseline or {}).get("operation_id", ""),
        "http_method": method.upper(),
        "host": hosts[0] if hosts else (baseline or {}).get("host", ""),
        "hosts": hosts,
        "path": path,
        "section": (baseline or {}).get("section") or ((op.get("tags") or [""])[0]),
        "scope": (baseline or {}).get("scope") or (op.get("x-category") or ("seller" if marketplace == "ozon_seller" else "performance" if marketplace == "ozon_performance" else "")),
        "safety": safety,
        "summary": op.get("summary") or (baseline or {}).get("summary", ""),
        "params": (baseline or {}).get("params") or {},
        "pagination": (baseline or {}).get("pagination") or infer_pagination(parameters)["mode"],
        "pagination_inference": infer_pagination(parameters),
        "items_path": (baseline or {}).get("items_path", ""),
        "rate_limit": (baseline or {}).get("rate_limit", ""),
        "documentation": (baseline or {}).get("documentation", ""),
        "keywords": (baseline or {}).get("keywords", []),
        "deprecated": deprecated,
        "contract": {
            "parameters": parameters,
            "request_body": normalize_request_body(spec, op),
            "responses": normalize_responses(spec, op),
        },
        "verification": {
            "implementation_inventory": bool(baseline),
            "current_openapi_snapshot": True,
            "official_contract_verified": False,
            "mirror_contract_verified": True,
        },
        "implementation_source": "runtime baseline 07a3408" if baseline else "not_in_runtime_baseline",
        "openapi_source_file": source_file,
    }


def build_wb(files: list[dict] | dict, baseline_index: dict) -> tuple[list[dict], dict[str, str]]:
    rows = []
    hashes = {}
    file_entries = list(files.keys()) if isinstance(files, dict) else list(files)
    for file_info in file_entries:
        filename = file_info["file"] if isinstance(file_info, dict) else str(file_info)
        spec, sha = fetch_yaml(f"{WB_REPO}/{filename}")
        hashes[filename] = sha
        for path, path_item in (spec.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            for method in HTTP_METHODS:
                op = path_item.get(method)
                if isinstance(op, dict):
                    rows.append(operation_record(
                        spec=spec, path=path, path_item=path_item, method=method, op=op,
                        marketplace="wildberries", source_file=filename,
                        baseline=baseline_index.get((method.upper(), path)),
                    ))
    return sorted(rows, key=lambda r: (r["path"], r["http_method"])), hashes


def build_ozon(spec: dict, marketplace: str, baseline_index: dict, source_file: str) -> list[dict]:
    rows = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if isinstance(op, dict):
                rows.append(operation_record(
                    spec=spec, path=path, path_item=path_item, method=method, op=op,
                    marketplace=marketplace, source_file=source_file,
                    baseline=baseline_index.get((method.upper(), path)),
                ))
    return sorted(rows, key=lambda r: (r["path"], r["http_method"]))


def load_status_overrides() -> dict[tuple[str, str, str], dict]:
    doc = yaml.safe_load((REG / "status_overrides.yaml").read_text(encoding="utf-8")) or {}
    result = {}
    for item in doc.get("entries") or []:
        marketplace = item.get("marketplace") or ""
        if marketplace == "ozon":
            marketplace = "ozon_seller"
        result[(marketplace, str(item.get("method") or "").upper(), item.get("path") or "")] = item
    return result


def apply_lifecycle(rows: list[dict], overrides: dict) -> None:
    for row in rows:
        key = (row["marketplace"], row["http_method"], row["path"])
        override = overrides.get(key)
        if override:
            status = override.get("status") or "CURRENT"
            row["lifecycle"] = {
                "status": status,
                "shutdown_date": override.get("shutdown_date") or "",
                "replacement": override.get("replacement") or "",
                "evidence": override.get("evidence") or [],
            }
        else:
            status = "DEPRECATED" if row.get("deprecated") else "CURRENT"
            row["lifecycle"] = {"status": status, "shutdown_date": "", "replacement": "", "evidence": []}
        row["status"] = status


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = ["marketplace", "operation_id", "http_method", "host", "path", "section", "scope", "safety", "summary", "pagination", "rate_limit", "status", "implemented_in_baseline", "contract_parameters", "request_body", "replacement", "shutdown_date"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            life = row["lifecycle"]
            writer.writerow({
                "marketplace": row["marketplace"], "operation_id": row["operation_id"],
                "http_method": row["http_method"], "host": row["host"], "path": row["path"],
                "section": row["section"], "scope": row["scope"], "safety": row["safety"],
                "summary": row["summary"], "pagination": row["pagination"], "rate_limit": row["rate_limit"],
                "status": row["status"], "implemented_in_baseline": row["verification"]["implementation_inventory"],
                "contract_parameters": json.dumps(row["contract"]["parameters"], ensure_ascii=False, separators=(",", ":")),
                "request_body": json.dumps(row["contract"]["request_body"], ensure_ascii=False, separators=(",", ":")),
                "replacement": json.dumps(life["replacement"], ensure_ascii=False, separators=(",", ":")) if isinstance(life["replacement"], (dict, list)) else life["replacement"],
                "shutdown_date": life["shutdown_date"],
            })


def main() -> None:
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    wb_index, wb_index_sha = fetch_json(URLS["wb_index"])
    wb_meta, wb_meta_sha = fetch_json(URLS["wb_meta"])
    ozon_seller_spec, ozon_seller_sha = fetch_json(URLS["ozon_seller"])
    ozon_perf_spec, ozon_perf_sha = fetch_json(URLS["ozon_performance"])

    baseline_rows = {}
    baseline_indexes = {}
    for name, path in BASELINE.items():
        rows, index = baseline_inventory(path, name)
        baseline_rows[name], baseline_indexes[name] = rows, index

    wb_files = wb_meta.get("files") or wb_index.get("files") or []
    wb, wb_file_hashes = build_wb(wb_files, baseline_indexes["wildberries"])
    current = {
        "wildberries": wb,
        "ozon_seller": build_ozon(ozon_seller_spec, "ozon_seller", baseline_indexes["ozon_seller"], "ozon-seller-openapi.json"),
        "ozon_performance": build_ozon(ozon_perf_spec, "ozon_performance", baseline_indexes["ozon_performance"], "ozon-performance-openapi.json"),
    }
    overrides = load_status_overrides()
    for rows in current.values():
        apply_lifecycle(rows, overrides)
        for row in rows:
            row["verification"]["official_contract_verified"] = bool(row["lifecycle"]["evidence"]) or (row["marketplace"] == "wildberries" and row["operation_id"] == "getV1SupplierOrders")

    for name, rows in current.items():
        write_json(CURRENT / f"{name}.json", rows)
    combined = [row for name in ("wildberries", "ozon_seller", "ozon_performance") for row in current[name]]
    write_json(REG / "api_catalog.json", combined)
    write_csv(REG / "api_catalog.csv", combined)

    diffs = {}
    for name, rows in current.items():
        cur_keys = {(r["http_method"], r["path"]) for r in rows}
        base_keys = {(r["method"], r["path"]) for r in baseline_rows[name]}
        diffs[name] = {
            "baseline_count": len(baseline_rows[name]),
            "current_snapshot_count": len(rows),
            "current_status_counts": {status: sum(r["status"] == status for r in rows) for status in ("CURRENT", "DEPRECATED", "SHUTDOWN")},
            "safety_counts": {safety: sum(r["safety"] == safety for r in rows) for safety in sorted({r["safety"] for r in rows})},
            "implemented_current_count": sum(r["verification"]["implementation_inventory"] for r in rows),
            "current_not_in_baseline": [{"method": m, "path": p} for m, p in sorted(cur_keys - base_keys)],
            "baseline_not_in_current": [{"method": m, "path": p} for m, p in sorted(base_keys - cur_keys)],
        }

    status_counts = {status: sum(r["status"] == status for r in combined) for status in ("CURRENT", "DEPRECATED", "SHUTDOWN")}
    summary = {
        "generated_at": generated_at,
        "baseline_app_commit": "07a3408eab9a2abe4ba1e9dbcfdd8c67bed0458d",
        "baseline_total": sum(len(v) for v in baseline_rows.values()),
        "current_snapshot_total": len(combined),
        "effective_status_counts": status_counts,
        "records_with_full_openapi_contract": sum(bool(r["contract"]) for r in combined),
        "records_implemented_in_baseline": sum(r["verification"]["implementation_inventory"] for r in combined),
        "records_safety_unverified": sum(r["safety"] == "UNVERIFIED" for r in combined),
        "wildberries_official_snapshot_date": wb_meta.get("snapshot_date"),
        "diffs": diffs,
    }
    write_json(AUDIT / "summary.json", summary)

    provenance = {
        "generated_at": generated_at,
        "policy": "mirror snapshots enumerate official OpenAPI contracts; official notices/runtime are authoritative for lifecycle-critical changes",
        "sources": {
            "wildberries": {
                "official_origin": "dev.wildberries.ru OpenAPI/Swagger",
                "mirror": "ZloyDeDD/wb-api-skill", "mirror_snapshot_date": wb_meta.get("snapshot_date"),
                "index_url": URLS["wb_index"], "index_sha256": wb_index_sha,
                "meta_url": URLS["wb_meta"], "meta_sha256": wb_meta_sha,
                "swagger_file_sha256": wb_file_hashes,
            },
            "ozon_seller": {"official_origin": "https://docs.ozon.ru/api/seller/swagger.json", "mirror": "MissiaL/ozon-api", "url": URLS["ozon_seller"], "sha256": ozon_seller_sha},
            "ozon_performance": {"official_origin": "https://docs.ozon.ru/api/performance/swagger.json", "mirror": "MissiaL/ozon-api", "url": URLS["ozon_performance"], "sha256": ozon_perf_sha},
        },
    }
    write_json(SOURCES / "SOURCES.json", provenance)

    print(json.dumps({
        "baseline_total": summary["baseline_total"], "current_snapshot_total": len(combined),
        "effective_status_counts": status_counts, "records_with_full_openapi_contract": summary["records_with_full_openapi_contract"],
        "records_implemented_in_baseline": summary["records_implemented_in_baseline"], "records_safety_unverified": summary["records_safety_unverified"],
        "counts": {name: len(rows) for name, rows in current.items()},
        "new_vs_baseline": {name: len(d["current_not_in_baseline"]) for name, d in diffs.items()},
        "baseline_not_in_current": {name: len(d["baseline_not_in_current"]) for name, d in diffs.items()},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
