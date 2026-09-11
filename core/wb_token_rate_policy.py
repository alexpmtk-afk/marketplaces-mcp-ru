"""Wildberries request-rate policy for token-type dependent endpoints.

WB encodes token type in the public JWT payload field ``acc``:
1=Base, 2=Test, 3=Personal, 4=Service.  Some WB endpoints use a much slower
quota for Base tokens.  This module adjusts only limits explicitly documented
as token-type dependent; every other endpoint keeps its catalog contract.

No token, seller id or other JWT claim is returned or logged.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Optional

WB_TOKEN_TYPES = {
    1: "base",
    2: "test",
    3: "personal",
    4: "service",
}

# Current WB documentation: Base token limits introduced for the legacy
# Statistics feeds. Personal/Service continue to use the catalog 1 req/min.
_WB_BASE_RATE_LIMITS = {
    "wb_stats_orders": "1 req/3h",
    "wb_stats_sales": "1 req/2h",
}


def wb_token_type(token: str) -> Optional[str]:
    """Return the WB access-token type from JWT ``acc`` without verification.

    The payload is used only for local quota selection. Authentication itself
    remains the provider's responsibility; malformed/unknown JWTs return None.
    """
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return WB_TOKEN_TYPES.get(int(claims.get("acc", 0) or 0))
    except (
        IndexError,
        ValueError,
        TypeError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None


def effective_wb_rate_limit(
    operation_id: Optional[str],
    catalog_rate_limit: str,
    token: str,
) -> str:
    """Return the safe catalog rate for this WB token and operation.

    Unknown/malformed token types keep the catalog contract. They will still be
    authenticated by WB; this helper never weakens a known stricter Base limit.
    """
    if wb_token_type(token) == "base" and operation_id:
        return _WB_BASE_RATE_LIMITS.get(operation_id, catalog_rate_limit)
    return catalog_rate_limit
