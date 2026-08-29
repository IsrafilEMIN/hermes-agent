from __future__ import annotations

import base64
import calendar
import json
import math
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

from agent.account_usage import (
    AccountUsageSnapshot,
    AccountUsageWindow,
    _parse_dt,
    _utc_now,
    httpx,
)

DEFAULT_CURSOR_BASE_URL = "https://api2.cursor.sh"
CURSOR_USAGE_SUMMARY_URL = "https://cursor.com/api/usage-summary"

_USED_BUCKET_KEYS = ("numRequests", "used", "amountUsed", "usdUsed")
_LIMIT_BUCKET_KEYS = ("maxRequestUsage", "limit", "amountLimit", "usdLimit")
_RESET_KEYS = ("billingCycleEnd", "endOfMonth", "resetsAt", "nextReset")
_START_KEYS = ("startOfMonth", "billingCycleStart", "startOfBillingCycle")


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, str) and value.strip():
        try:
            numeric = float(value.strip())
        except ValueError:
            return None
        return numeric if math.isfinite(numeric) else None
    return None


def _clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def _first_number(bucket: dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    for key in keys:
        value = _finite_number(bucket.get(key))
        if value is not None:
            return value
    return None


def _decode_jwt_payload(token: str) -> Optional[dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    encoded = parts[1].replace("-", "+").replace("_", "/")
    encoded += "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.b64decode(encoded))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_cursor_user_id(token: str) -> Optional[str]:
    payload = _decode_jwt_payload(token)
    sub = payload.get("sub") if payload else None
    if not isinstance(sub, str):
        return None
    parts = sub.split("|")
    user_id = (parts[1] if len(parts) > 1 else sub).strip()
    return user_id or None


def _cursor_timestamp_to_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        return _parse_dt(numeric / 1000.0 if numeric >= 1e12 else numeric)
    return _parse_dt(value)


def _add_one_month(dt: datetime) -> datetime:
    index = dt.year * 12 + dt.month - 1 + 1
    year, month_index = divmod(index, 12)
    month = month_index + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _derive_cursor_resets_at(payload: Any) -> Optional[datetime]:
    if not isinstance(payload, dict):
        return None
    for key in _RESET_KEYS:
        dt = _cursor_timestamp_to_dt(payload.get(key))
        if dt is not None:
            return dt
    for key in _START_KEYS:
        dt = _cursor_timestamp_to_dt(payload.get(key))
        if dt is not None:
            return _add_one_month(dt)
    return None


def _monthly_window(
    label: str,
    used_percent: float,
    resets_at: Optional[datetime],
    window_id: str = "monthly",
) -> AccountUsageWindow:
    return AccountUsageWindow(label=label, used_percent=used_percent, reset_at=resets_at, id=window_id)


_USABLE_NO_PERCENT = object()


def _parse_cursor_cents_bucket(bucket: dict[str, Any]) -> Any:
    if bucket.get("enabled") is False:
        return None
    reported_used = _finite_number(bucket.get("used"))
    reported_remaining = _finite_number(bucket.get("remaining"))
    has_valid_used = reported_used is not None and reported_used >= 0
    has_valid_remaining = reported_remaining is not None and reported_remaining >= 0
    if bucket.get("limit") is None:
        return _USABLE_NO_PERCENT if has_valid_used else None
    limit = _finite_number(bucket.get("limit"))
    if limit is None or limit <= 0:
        return None
    if reported_used is not None and reported_used > 0:
        used = reported_used
    elif has_valid_remaining and reported_remaining < limit:
        used = max(0.0, limit - reported_remaining)
    elif has_valid_used:
        used = reported_used
    else:
        return None
    return _clamp_percent(used / limit * 100.0)


def _cents_used_percent(bucket: dict[str, Any]) -> Optional[float]:
    amount = _parse_cursor_cents_bucket(bucket)
    if amount is None or amount is _USABLE_NO_PERCENT:
        return None
    return amount


def _plan_dashboard_windows(bucket: dict[str, Any], resets_at: Optional[datetime]) -> list[AccountUsageWindow]:
    if bucket.get("enabled") is False:
        return []
    auto_percent = _finite_number(bucket.get("autoPercentUsed"))
    api_percent = _finite_number(bucket.get("apiPercentUsed"))
    windows: list[AccountUsageWindow] = []
    if auto_percent is not None:
        windows.append(_monthly_window("Cursor Models", _clamp_percent(auto_percent), resets_at))
    if api_percent is not None:
        windows.append(_monthly_window("Other Models", _clamp_percent(api_percent), resets_at, "monthly_other"))
    if windows:
        return windows
    total_percent = _finite_number(bucket.get("totalPercentUsed"))
    if total_percent is not None:
        return [_monthly_window("Personal Usage", _clamp_percent(total_percent), resets_at)]
    cents_percent = _cents_used_percent(bucket)
    if cents_percent is not None:
        return [_monthly_window("Personal Usage", cents_percent, resets_at)]
    return []


def _parse_cursor_usage_summary(payload: Any) -> Optional[tuple[AccountUsageWindow, ...]]:
    if not isinstance(payload, dict):
        return None
    individual = payload.get("individualUsage")
    if not isinstance(individual, dict):
        return None
    resets_at = _derive_cursor_resets_at(payload)
    windows: list[AccountUsageWindow] = []
    overall = individual.get("overall")
    used_overall = False
    if isinstance(overall, dict):
        overall_amount = _parse_cursor_cents_bucket(overall)
        if overall_amount is not None:
            used_overall = True
            if overall_amount is not _USABLE_NO_PERCENT:
                windows.append(_monthly_window("Personal Usage", overall_amount, resets_at))
    plan = individual.get("plan")
    if not used_overall and isinstance(plan, dict):
        windows.extend(_plan_dashboard_windows(plan, resets_at))
    if not windows:
        on_demand = individual.get("onDemand")
        if isinstance(on_demand, dict):
            on_demand_percent = _cents_used_percent(on_demand)
            if on_demand_percent is not None:
                windows.append(_monthly_window("On-Demand Usage", on_demand_percent, resets_at))
    if windows:
        return tuple(windows)
    return () if used_overall else None


def _parse_cursor_auth_usage(payload: Any) -> Optional[tuple[AccountUsageWindow, ...]]:
    if not isinstance(payload, dict):
        return None
    resets_at = _derive_cursor_resets_at(payload)
    for bucket in payload.values():
        if not isinstance(bucket, dict):
            continue
        if bucket.get("enabled") is False:
            continue
        used = _first_number(bucket, _USED_BUCKET_KEYS)
        limit = _first_number(bucket, _LIMIT_BUCKET_KEYS)
        if used is None or limit is None or limit <= 0:
            continue
        return (
            _monthly_window("Monthly", _clamp_percent(used / limit * 100.0), resets_at),
        )
    return None


def fetch_cursor_account_usage(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    """Fetch Cursor quota usage for the chip, fail-open.

    Requires an explicit api_key (OAuth/JWT or API token); queries Cursor's
    usage-summary and /auth/usage endpoints and maps the result onto
    monthly / monthly_other windows. Returns None when there is nothing
    usable to show.
    """
    token = (api_key or "").strip()
    if not token:
        return None
    base = (base_url or DEFAULT_CURSOR_BASE_URL).strip().rstrip("/")
    user_id = _extract_cursor_user_id(token)
    summary_windows = None
    legacy_windows = None
    with httpx.Client(timeout=15.0) as client:
        if user_id is not None and base == DEFAULT_CURSOR_BASE_URL:
            try:
                response = client.get(
                    CURSOR_USAGE_SUMMARY_URL,
                    headers={
                        "Accept": "application/json",
                        "Cookie": "WorkosCursorSessionToken=" + quote(f"{user_id}::{token}", safe="-_.!~*'()"),
                    },
                )
                response.raise_for_status()
                summary_windows = _parse_cursor_usage_summary(response.json())
            except (httpx.HTTPError, ValueError):
                summary_windows = None
        try:
            response = client.get(
                f"{base}/auth/usage",
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            legacy_windows = _parse_cursor_auth_usage(response.json())
        except (httpx.HTTPError, ValueError):
            legacy_windows = None
    windows = summary_windows if summary_windows is not None else legacy_windows
    if not windows:
        return None
    return AccountUsageSnapshot(
        provider="cursor",
        source="usage_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
    )
