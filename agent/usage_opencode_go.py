from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any, Optional

import httpx

from agent.account_usage import (
    AccountUsageSnapshot,
    AccountUsageWindow,
    _parse_dt,
    _utc_now,
)

_DEFAULT_BASE_URL = "https://opencode.ai/zen/go"
_USAGE_PATH = "/v1/usage"
_PROVIDER = "opencode-go"
_PLAN = "OpenCode Go"
_WINDOWS = (
    ("rolling", "5 Hour", "5h"),
    ("weekly", "Weekly", "7d"),
    ("monthly", "Monthly", "monthly"),
)


def _normalize_base_url(base_url: Optional[str]) -> str:
    if not base_url or not str(base_url).strip():
        return _DEFAULT_BASE_URL
    normalized = str(base_url).strip().rstrip("/")
    if normalized.lower().endswith("/v1"):
        normalized = normalized[: -len("/v1")]
    return normalized or _DEFAULT_BASE_URL


def _decode_window(payload: Any) -> Optional[tuple[float, datetime]]:
    if not isinstance(payload, dict):
        return None
    percent = payload.get("percent")
    if (
        not isinstance(percent, (int, float))
        or isinstance(percent, bool)
        or not math.isfinite(percent)
        or percent < 0
        or percent > 100
    ):
        return None
    if payload.get("status") not in ("ok", "rate-limited"):
        return None
    reset_at = _parse_dt(payload.get("resetsAt"))
    if reset_at is None:
        return None
    return float(percent), reset_at


def fetch_opencode_go_account_usage(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    """Fetch OpenCode Go account usage from the /zen/go/v1/usage endpoint."""
    key = (api_key or os.environ.get("OPENCODE_GO_API_KEY") or "").strip()
    if not key:
        return None
    resolved_base_url = base_url or os.environ.get("OPENCODE_GO_BASE_URL")
    url = f"{_normalize_base_url(resolved_base_url)}{_USAGE_PATH}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=headers)
    except Exception:
        return None
    if response.status_code < 200 or response.status_code >= 300:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    windows = []
    for window_key, label, window_id in _WINDOWS:
        decoded = _decode_window(usage.get(window_key))
        if decoded is None:
            return None
        percent, reset_at = decoded
        windows.append(AccountUsageWindow(label=label, used_percent=percent, reset_at=reset_at, id=window_id))
    return AccountUsageSnapshot(
        provider=_PROVIDER,
        source="usage_api",
        fetched_at=_utc_now(),
        plan=_PLAN,
        windows=tuple(windows),
    )
