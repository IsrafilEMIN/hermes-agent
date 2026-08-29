from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow, _parse_dt, _utc_now
from hermes_cli.auth import AuthError

logger = logging.getLogger(__name__)

_WEEKLY_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
_MONTHLY_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"


@dataclass(frozen=True)
class _WeeklyBilling:
    start: datetime
    end: datetime
    credit_usage_percent: float
    inferred_percent: bool


@dataclass(frozen=True)
class _MonthlyBilling:
    start: datetime
    end: datetime
    used: float
    limit: float


def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _parse_percent(value: Any) -> Optional[float]:
    number = _to_number(value)
    if number is None or number < 0 or number > 100:
        return None
    return number


def _parse_on_demand_amount(value: Any) -> Optional[float]:
    if not isinstance(value, dict):
        return None
    number = _to_number(value.get("val"))
    if number is None or number < 0:
        return None
    return number


def _parse_weekly_billing_config(raw: dict) -> Optional[_WeeklyBilling]:
    period = raw.get("currentPeriod")
    if not isinstance(period, dict):
        return None
    start_raw = period.get("start")
    end_raw = period.get("end")
    start = _parse_dt(start_raw) if isinstance(start_raw, str) else None
    end = _parse_dt(end_raw) if isinstance(end_raw, str) else None
    period_type = period.get("type")
    period_type = period_type if isinstance(period_type, str) else ""
    if start is None or end is None or end <= start or "WEEK" not in period_type.upper():
        return None
    inferred = raw.get("creditUsagePercent") is None
    if inferred:
        percent = 0.0 if end > _utc_now() else None
    else:
        percent = _parse_percent(raw.get("creditUsagePercent"))
    if percent is None:
        return None
    return _WeeklyBilling(
        start=start,
        end=end,
        credit_usage_percent=percent,
        inferred_percent=inferred,
    )


def _parse_monthly_billing_config(raw: dict) -> Optional[_MonthlyBilling]:
    start_raw = raw.get("billingPeriodStart")
    end_raw = raw.get("billingPeriodEnd")
    start = _parse_dt(start_raw) if isinstance(start_raw, str) else None
    end = _parse_dt(end_raw) if isinstance(end_raw, str) else None
    if start is None or end is None or end <= start:
        return None
    limit = _parse_on_demand_amount(raw.get("monthlyLimit"))
    used = _parse_on_demand_amount(raw.get("used"))
    if limit is None or limit <= 0 or used is None:
        return None
    return _MonthlyBilling(start=start, end=end, used=used, limit=limit)


def _confirms_no_monthly_quota(raw: dict) -> bool:
    limit = _parse_on_demand_amount(raw.get("monthlyLimit"))
    if limit is not None:
        return limit == 0
    weekly = _parse_weekly_billing_config(raw)
    return weekly is not None and weekly.inferred_percent


def _fetch_billing_payload(client: httpx.Client, url: str, headers: dict) -> Any:
    try:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def fetch_xai_oauth_account_usage(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    """Return a SuperGrok subscription usage snapshot from the Grok CLI billing
    endpoint, or None when credentials are missing or the fetch fails.
    """
    token = str(api_key or "").strip()
    if not token:
        try:
            from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

            token = str(
                resolve_xai_oauth_runtime_credentials(refresh_if_expiring=True).get("api_key", "") or ""
            ).strip()
        except AuthError:
            return None
    if not token:
        return None
    try:
        with httpx.Client(timeout=15.0) as client:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "X-XAI-Token-Auth": "xai-grok-cli",
            }
            credits_payload = _fetch_billing_payload(client, _WEEKLY_BILLING_URL, headers)
            weekly = None
            credits_looks_unified = False
            credits_config = credits_payload.get("config") if isinstance(credits_payload, dict) else None
            if isinstance(credits_config, dict):
                weekly = _parse_weekly_billing_config(credits_config)
                credits_looks_unified = credits_config.get("isUnifiedBillingUser") is True

            monthly = None
            monthly_config = None
            if (weekly is None or credits_looks_unified) and _MONTHLY_BILLING_URL != _WEEKLY_BILLING_URL:
                monthly_payload = _fetch_billing_payload(client, _MONTHLY_BILLING_URL, headers)
                monthly_config = monthly_payload.get("config") if isinstance(monthly_payload, dict) else None
                if isinstance(monthly_config, dict):
                    monthly = _parse_monthly_billing_config(monthly_config)

            effective_weekly = weekly
            if weekly is not None and weekly.inferred_percent and credits_looks_unified:
                if monthly is not None:
                    effective_weekly = None
                elif monthly_config is None or not _confirms_no_monthly_quota(monthly_config):
                    effective_weekly = None
            if effective_weekly is None and monthly is None:
                return None

            windows = []
            if effective_weekly is not None:
                windows.append(
                    AccountUsageWindow(
                        label="Weekly",
                        used_percent=effective_weekly.credit_usage_percent,
                        reset_at=effective_weekly.end,
                        id="7d",
                    )
                )
            if monthly is not None:
                windows.append(
                    AccountUsageWindow(
                        label="Monthly",
                        used_percent=monthly.used / monthly.limit * 100,
                        reset_at=monthly.end,
                        id="monthly",
                    )
                )
            return AccountUsageSnapshot(
                provider="xai-oauth",
                source="cli_billing",
                fetched_at=_utc_now(),
                windows=tuple(windows),
            )
    except Exception:
        logger.debug("xai-oauth ▸ billing fetch failed (fail-open)", exc_info=True)
        return None
