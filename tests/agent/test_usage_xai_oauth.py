from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agent import usage_xai_oauth
from hermes_cli import auth


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _future(days=7):
    return datetime.now(timezone.utc) + timedelta(days=days)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            request = httpx.Request("GET", "https://cli-chat-proxy.grok.com/v1/billing")
            raise httpx.HTTPStatusError(
                f"HTTP {self._status}",
                request=request,
                response=httpx.Response(self._status, request=request),
            )

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, routes, calls):
        self._routes = routes
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        for fragment, (payload, status) in self._routes.items():
            if fragment in url:
                return _FakeResponse(payload, status)
        return _FakeResponse(None, 404)


def _patch_billing(monkeypatch, routes):
    calls = []
    monkeypatch.setattr(usage_xai_oauth.httpx, "Client", lambda **kwargs: _FakeClient(routes, calls))
    return calls


def _weekly_payload(percent=None, unified=False, end=None):
    config = {
        "currentPeriod": {
            "start": _ts(datetime.now(timezone.utc) - timedelta(days=7)),
            "end": _ts(end if end is not None else _future()),
            "type": "WEEK",
        }
    }
    if percent is not None:
        config["creditUsagePercent"] = percent
    if unified:
        config["isUnifiedBillingUser"] = True
    return {"config": config}


def _monthly_payload(used=20, limit=100):
    return {
        "config": {
            "billingPeriodStart": "2026-08-01T00:00:00Z",
            "billingPeriodEnd": "2026-09-01T00:00:00Z",
            "monthlyLimit": {"val": limit},
            "used": {"val": used},
        }
    }


def test_weekly_credits_payload_yields_seven_day_window(monkeypatch):
    end = _future()
    payload = _weekly_payload(percent=10, end=end)
    calls = _patch_billing(monkeypatch, {"format=credits": (payload, 200)})

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is not None
    assert snapshot.provider == "xai-oauth"
    assert snapshot.source == "cli_billing"
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.id == "7d"
    assert window.label == "Weekly"
    assert window.used_percent == 10.0
    assert window.reset_at == end.replace(microsecond=0)
    assert len(calls) == 1
    assert "format=credits" in calls[0]["url"]
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert calls[0]["headers"]["Accept"] == "application/json"
    assert calls[0]["headers"]["X-XAI-Token-Auth"] == "xai-grok-cli"


def test_unified_monthly_quota_used_when_weekly_percent_omitted(monkeypatch):
    weekly = _weekly_payload(unified=True, end=_future())
    monthly = _monthly_payload(used=20, limit=100)
    calls = _patch_billing(
        monkeypatch,
        {"format=credits": (weekly, 200), "v1/billing": (monthly, 200)},
    )

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is not None
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.id == "monthly"
    assert window.label == "Monthly"
    assert window.used_percent == pytest.approx(20.0)
    assert window.reset_at == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert len(calls) == 2
    assert "format=credits" in calls[0]["url"]
    assert "format" not in calls[1]["url"]


def test_explicit_api_key_skips_resolver(monkeypatch):
    def boom(**kwargs):
        raise AssertionError("resolver must not be called")

    monkeypatch.setattr(auth, "resolve_xai_oauth_runtime_credentials", boom)
    payload = _weekly_payload(percent=10, end=_future())
    calls = _patch_billing(monkeypatch, {"format=credits": (payload, 200)})

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="explicit-token")

    assert snapshot is not None
    assert calls[0]["headers"]["Authorization"] == "Bearer explicit-token"


def test_missing_token_returns_none(monkeypatch):
    monkeypatch.setattr(auth, "resolve_xai_oauth_runtime_credentials", lambda **kwargs: {"api_key": ""})
    calls = _patch_billing(monkeypatch, {})

    assert usage_xai_oauth.fetch_xai_oauth_account_usage() is None
    assert calls == []


def test_auth_error_returns_none(monkeypatch):
    def boom(**kwargs):
        raise auth.AuthError("no xai oauth creds")

    monkeypatch.setattr(auth, "resolve_xai_oauth_runtime_credentials", boom)
    calls = _patch_billing(monkeypatch, {})

    assert usage_xai_oauth.fetch_xai_oauth_account_usage() is None
    assert calls == []


def test_http_401_returns_none(monkeypatch):
    calls = _patch_billing(monkeypatch, {"format=credits": ({"config": {}}, 401)})

    assert usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token") is None
    assert len(calls) == 2


def test_weekly_http_error_still_probes_monthly(monkeypatch):
    monthly = _monthly_payload(used=20, limit=100)
    calls = _patch_billing(
        monkeypatch,
        {"format=credits": ({"config": {}}, 401), "v1/billing": (monthly, 200)},
    )

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is not None
    assert [window.id for window in snapshot.windows] == ["monthly"]
    assert snapshot.windows[0].used_percent == pytest.approx(20.0)
    assert len(calls) == 2


def test_monthly_http_error_keeps_explicit_weekly(monkeypatch):
    weekly = _weekly_payload(percent=10, unified=True, end=_future())
    calls = _patch_billing(
        monkeypatch,
        {"format=credits": (weekly, 200), "v1/billing": ({}, 500)},
    )

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is not None
    assert [window.id for window in snapshot.windows] == ["7d"]
    assert snapshot.windows[0].used_percent == 10.0
    assert len(calls) == 2


def test_unified_inferred_weekly_dropped_when_monthly_unparseable(monkeypatch):
    weekly = _weekly_payload(unified=True, end=_future())
    monthly = {
        "config": {
            "billingPeriodStart": "2026-08-01T00:00:00Z",
            "billingPeriodEnd": "2026-08-01T00:00:00Z",
            "monthlyLimit": {"val": 100},
            "used": {"val": 20},
        }
    }
    calls = _patch_billing(
        monkeypatch,
        {"format=credits": (weekly, 200), "v1/billing": (monthly, 200)},
    )

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is None
    assert len(calls) == 2


def test_unified_inferred_weekly_kept_when_monthly_confirms_zero_quota(monkeypatch):
    weekly = _weekly_payload(unified=True, end=_future())
    monthly = {"config": {"monthlyLimit": {"val": 0}}}
    calls = _patch_billing(
        monkeypatch,
        {"format=credits": (weekly, 200), "v1/billing": (monthly, 200)},
    )

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is not None
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.id == "7d"
    assert window.used_percent == 0.0


def test_both_windows_when_weekly_percent_explicit_and_monthly_parses(monkeypatch):
    weekly = _weekly_payload(percent=30, unified=True, end=_future())
    monthly = _monthly_payload(used=20, limit=100)
    calls = _patch_billing(
        monkeypatch,
        {"format=credits": (weekly, 200), "v1/billing": (monthly, 200)},
    )

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is not None
    assert [window.id for window in snapshot.windows] == ["7d", "monthly"]
    assert snapshot.windows[0].used_percent == 30.0
    assert snapshot.windows[1].used_percent == pytest.approx(20.0)
    assert len(calls) == 2


def test_expired_period_without_percent_is_rejected(monkeypatch):
    weekly = _weekly_payload(end=datetime.now(timezone.utc) - timedelta(days=1))
    calls = _patch_billing(monkeypatch, {"format=credits": (weekly, 200)})

    snapshot = usage_xai_oauth.fetch_xai_oauth_account_usage(api_key="test-token")

    assert snapshot is None
    assert len(calls) == 2
