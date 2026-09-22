#!/usr/bin/env python3
"""Safe production check for the shared marketplace rate-limit backend."""
from __future__ import annotations

import argparse
import json
import sys

from core.rate_limit import configured_global_rps, redis_url_from_env, verify_shared_redis
from core.rate_limit_repair import repair_legacy_wb_global_cooldowns


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-redis", action="store_true")
    parser.add_argument("--repair-legacy-wb", action="store_true")
    args = parser.parse_args()

    backend = "redis" if redis_url_from_env() else "sqlite"
    if args.require_redis:
        try:
            verify_shared_redis()
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({
                "ok": False,
                "backend": backend,
                "error": type(exc).__name__,
                "message": str(exc),
            }, ensure_ascii=False))
            return 2

    repaired = 0
    if args.repair_legacy_wb:
        try:
            repaired = repair_legacy_wb_global_cooldowns()
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({
                "ok": False,
                "backend": backend,
                "error": type(exc).__name__,
                "message": str(exc),
            }, ensure_ascii=False))
            return 3

    result = {
        "ok": True,
        "backend": backend,
        "wb_global_rps": configured_global_rps("wb"),
        "ozon_global_rps": configured_global_rps("ozon"),
        "ozon_perf_global_rps": configured_global_rps("ozon_perf"),
        "legacy_wb_global_slots_removed": repaired,
    }
    if result["wb_global_rps"] is not None:
        result["ok"] = False
        result["error"] = "WB_GLOBAL_BUCKET_MUST_BE_DISABLED"
        print(json.dumps(result, ensure_ascii=False))
        return 4
    if args.require_redis and backend != "redis":
        result["ok"] = False
        result["error"] = "REDIS_REQUIRED"
        print(json.dumps(result, ensure_ascii=False))
        return 5

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
