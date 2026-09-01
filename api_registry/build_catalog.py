from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_DIR = ROOT / "api_registry"
DEFAULT_OUTPUT = REGISTRY_DIR / "api_catalog.json"

SOURCES = [
    ("wildberries", ROOT / "wb_mcp" / "endpoints.yaml"),
    ("ozon", ROOT / "ozon_mcp" / "endpoints.yaml"),
    ("ozon_performance", ROOT / "ozon_mcp" / "perf_endpoints.yaml"),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected YAML mapping")
    return data


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _normalize(marketplace: str, source_path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    operation_id = str(raw.get("operation_id", "")).strip()
    if not operation_id:
        raise ValueError(f"{source_path}: endpoint without operation_id")
    return {
        "marketplace": marketplace,
        "operation_id": operation_id,
        "section": raw.get("section"),
        "http_method": raw.get("method"),
        "host": raw.get("host"),
        "path": raw.get("path"),
        "scope": raw.get("scope"),
        "safety": raw.get("safety"),
        "summary": raw.get("summary"),
        "params": raw.get("params"),
        "pagination": raw.get("pagination"),
        "items_path": raw.get("items_path"),
        "rate_limit": raw.get("rate_limit"),
        "keywords": raw.get("keywords") or [],
        "documentation": raw.get("doc"),
        "implementation_source": str(source_path.relative_to(ROOT)).replace("\\", "/"),
        "verification": {
            "implementation_inventory": True,
            "official_contract_verified": False,
        },
    }


def build() -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    for marketplace, path in SOURCES:
        data = _load_yaml(path)
        endpoints = data.get("endpoints") or []
        if not isinstance(endpoints, list):
            raise ValueError(f"{path}: endpoints must be a list")
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                raise ValueError(f"{path}: endpoint must be a mapping")
            records.append(_normalize(marketplace, path, endpoint))
        sources.append({
            "marketplace": marketplace,
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "sha256": _sha256(path),
            "endpoint_count": len(endpoints),
        })

    overrides_path = REGISTRY_DIR / "overrides.yaml"
    if overrides_path.exists():
        overrides_doc = _load_yaml(overrides_path)
        overrides = overrides_doc.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("api_registry/overrides.yaml: overrides must be a mapping")
        index = {(r["marketplace"], r["operation_id"]): i for i, r in enumerate(records)}
        for key, extra in overrides.items():
            if not isinstance(extra, dict) or ":" not in key:
                raise ValueError(f"Invalid override {key!r}")
            marketplace, operation_id = key.split(":", 1)
            idx = index.get((marketplace, operation_id))
            if idx is None:
                raise ValueError(f"Override points to unknown endpoint: {key}")
            records[idx] = _deep_merge(records[idx], extra)

    keys = [(r["marketplace"], r["operation_id"]) for r in records]
    duplicates = [f"{m}:{op}" for (m, op), count in Counter(keys).items() if count > 1]
    if duplicates:
        raise ValueError(f"Duplicate operation ids: {duplicates}")

    counts_by_marketplace = Counter(r["marketplace"] for r in records)
    counts_by_safety = Counter(str(r.get("safety")) for r in records)
    unverified = [
        f"{r['marketplace']}:{r['operation_id']}"
        for r in records
        if not r.get("verification", {}).get("official_contract_verified")
    ]

    return {
        "registry_version": "0.1-research",
        "purpose": "Normalized implementation inventory plus verified contract overrides; not a replacement for pinned official OpenAPI sources.",
        "sources": sources,
        "summary": {
            "total_endpoints": len(records),
            "by_marketplace": dict(sorted(counts_by_marketplace.items())),
            "by_safety": dict(sorted(counts_by_safety.items())),
            "official_contract_unverified_count": len(unverified),
        },
        "records": sorted(records, key=lambda r: (r["marketplace"], r["operation_id"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    payload = build()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    output = Path(args.output)

    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != rendered:
            print(f"API registry drift: regenerate {output}")
            return 1
        print(f"API registry OK: {payload['summary']['total_endpoints']} endpoints")
        return 0

    output.write_text(rendered, encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
