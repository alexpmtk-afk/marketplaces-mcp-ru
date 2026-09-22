#!/usr/bin/env python3
"""Production-safe preflight for the exact quota catalogs imported by Python."""
from __future__ import annotations

import json
import sys

from core.runtime_contracts import audit_runtime_contracts
from ozon_mcp import server as ozon
from wb_mcp import server as wb


def main() -> int:
    report = audit_runtime_contracts({"wb": wb, "ozon": ozon})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
