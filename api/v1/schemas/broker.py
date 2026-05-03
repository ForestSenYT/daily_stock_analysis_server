# -*- coding: utf-8 -*-
"""Pydantic request / response schemas for /api/v1/broker/firstrade/*.

The structures here intentionally do NOT model every field of the
underlying Firstrade payload; they expose only the masked / aggregated
shape that the WebUI and the agent tool consume. The full (already
redacted) snapshot is carried inside ``payload`` dicts so future
fields can be surfaced without breaking the schema.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# =====================================================================
# Requests
# =====================================================================

class FirstradeMfaVerifyRequest(BaseModel):
    """Body for POST /broker/firstrade/login/verify."""
    code: str = Field(..., min_length=4, max_length=12, description="MFA code")


class FirstradeSyncRequest(BaseModel):
    """Body for POST /broker/firstrade/sync (all fields optional)."""
    date_range: str = Field(
        default="today",
        description=(
            "Transaction history range: today / 1w / 1m / 2m / mtd / "
            "ytd / ly. Custom ranges are reserved but downgrade to "
            "today in v1."
        ),
    )


# =====================================================================
# Responses
# =====================================================================

class BrokerStatusResponse(BaseModel):
    """Returned by GET /broker/firstrade/status."""
    status: str
    broker: str = "firstrade"
    enabled: bool
    logged_in: Optional[bool] = None
    read_only: Optional[bool] = None
    last_sync: Optional[Dict[str, Any]] = None
    llm_data_scope: Optional[str] = None
    message: Optional[str] = None


class FirstradeLoginResponse(BaseModel):
    status: str
    broker: str = "firstrade"
    message: Optional[str] = None
    account_count: int = 0


class FirstradeSyncResponse(BaseModel):
    status: str
    broker: str = "firstrade"
    message: Optional[str] = None
    as_of: Optional[str] = None
    account_count: int = 0
    balance_count: int = 0
    position_count: int = 0
    order_count: int = 0
    transaction_count: int = 0


class BrokerListResponse(BaseModel):
    """Wrapper for accounts / positions / orders / transactions."""
    status: str
    broker: str = "firstrade"
    message: Optional[str] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)


class BrokerSnapshotResponse(BaseModel):
    """Full snapshot — what the agent tool and the WebUI use."""
    status: str
    broker: str = "firstrade"
    message: Optional[str] = None
    as_of: Optional[str] = None
    last_sync: Optional[Dict[str, Any]] = None
    accounts: List[Dict[str, Any]] = Field(default_factory=list)
    balances: List[Dict[str, Any]] = Field(default_factory=list)
    positions: List[Dict[str, Any]] = Field(default_factory=list)
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    transactions: List[Dict[str, Any]] = Field(default_factory=list)
