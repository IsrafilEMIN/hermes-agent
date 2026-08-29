import base64
import json
import urllib.parse

import httpx
import pytest

from agent import account_usage, usage_cursor


def _jwt(payload):
    def enc(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, by_url):
        self.calls = calls
        self.by_url = by_url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        for fragment, payload in self.by_url:
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(payload)
        raise AssertionError(f"no fake registered for {url}")


def _patch_client(monkeypatch, by_url):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, by_url),
    )
    return calls


def test_cursor_summary_plan_percent_windows_with_jwt(monkeypatch):
    token = _jwt({"sub": "auth0|u1"})
    summary = {
        "individualUsage": {
            "plan": {"autoPercentUsed": 6, "apiPercentUsed": 5},
        }
    }
    calls = _patch_client(
        monkeypatch,
        [
            ("api2.cursor.sh/auth/usage", {}),
            ("cursor.com/api/usage-summary", summary),
        ],
    )

    snapshot = usage_cursor.fetch_cursor_account_usage(api_key=token)

    assert snapshot is not None
    assert snapshot.provider == "cursor"
    assert snapshot.source == "usage_api"
    assert [(w.id, w.label, w.used_percent) for w in snapshot.windows] == [
        ("monthly", "Cursor Models", 6.0),
        ("monthly_other", "Other Models", 5.0),
    ]
    summary_call = next(c for c in calls if "usage-summary" in c["url"])
    assert summary_call["headers"]["Accept"] == "application/json"
    expected_cookie = "WorkosCursorSessionToken=" + urllib.parse.quote(f"u1::{token}", safe="-_.!~*'()")
    assert summary_call["headers"]["Cookie"] == expected_cookie
    legacy_call = next(c for c in calls if "auth/usage" in c["url"])
    assert legacy_call["headers"]["Authorization"] == f"Bearer {token}"


def test_cursor_auth_usage_bucket_without_jwt(monkeypatch):
    calls = _patch_client(
        monkeypatch,
        [("api2.cursor.sh/auth/usage", {"planUsage": {"used": 10, "limit": 100}})],
    )

    snapshot = usage_cursor.fetch_cursor_account_usage(api_key="plain-token")

    assert snapshot is not None
    assert [(w.id, w.used_percent) for w in snapshot.windows] == [("monthly", 10.0)]
    assert all("cursor.com" not in c["url"] for c in calls)


def test_cursor_missing_api_key_returns_none():
    assert usage_cursor.fetch_cursor_account_usage(api_key=None) is None
    assert usage_cursor.fetch_cursor_account_usage(api_key="   ") is None


def test_cursor_both_endpoints_fail_returns_none(monkeypatch):
    _patch_client(
        monkeypatch,
        [
            ("api2.cursor.sh/auth/usage", httpx.ConnectError("down")),
            ("cursor.com/api/usage-summary", httpx.ConnectError("down")),
        ],
    )

    snapshot = usage_cursor.fetch_cursor_account_usage(api_key=_jwt({"sub": "u1"}))

    assert snapshot is None


def _windows(payload):
    parsed = usage_cursor._parse_cursor_usage_summary(payload)
    return [(w.id, w.label, w.used_percent) for w in parsed] if parsed else None


def test_cursor_summary_prefers_overall_over_plan():
    assert _windows(
        {
            "individualUsage": {
                "overall": {"enabled": True, "used": 100, "limit": 1000, "remaining": 900},
                "plan": {"enabled": True, "used": 924, "limit": 7000, "autoPercentUsed": 1.85, "apiPercentUsed": 0},
            }
        }
    ) == [("monthly", "Personal Usage", 10.0)]


def test_cursor_summary_falls_back_to_plan_when_overall_disabled():
    assert _windows(
        {
            "individualUsage": {
                "overall": {"enabled": False, "used": 100, "limit": 1000, "remaining": 900},
                "plan": {"enabled": True, "autoPercentUsed": 1.85, "apiPercentUsed": 0},
            }
        }
    ) == [
        ("monthly", "Cursor Models", 1.85),
        ("monthly_other", "Other Models", 0.0),
    ]


def test_cursor_summary_rejects_disabled_plan():
    assert (
        _windows(
            {
                "individualUsage": {
                    "plan": {"enabled": False, "autoPercentUsed": 1.85, "apiPercentUsed": 0},
                }
            }
        )
        is None
    )


def test_cursor_summary_keeps_on_demand_when_plan_unusable():
    assert _windows(
        {
            "individualUsage": {
                "plan": {"enabled": False, "autoPercentUsed": 1.85},
                "onDemand": {"enabled": True, "used": 0, "limit": 2000, "remaining": 2000},
            }
        }
    ) == [("monthly", "On-Demand Usage", 0.0)]


def test_cursor_summary_parses_string_cents_and_remaining_fallback():
    assert _windows(
        {
            "individualUsage": {
                "overall": {"enabled": True, "used": "9000", "limit": "10000", "remaining": "1000"},
            }
        }
    ) == [("monthly", "Personal Usage", 90.0)]
    assert _windows(
        {
            "individualUsage": {
                "overall": {"used": 0, "limit": 5000, "remaining": 1500},
            }
        }
    ) == [("monthly", "Personal Usage", 70.0)]


def test_cursor_summary_uncapped_overall_skips_plan():
    assert (
        _windows(
            {
                "individualUsage": {
                    "overall": {"enabled": True, "used": "2500", "limit": None},
                    "plan": {"enabled": True, "autoPercentUsed": 1.85, "apiPercentUsed": 0},
                }
            }
        )
        is None
    )


def test_cursor_summary_uncapped_overall_keeps_on_demand():
    assert _windows(
        {
            "individualUsage": {
                "overall": {"enabled": True, "used": "2500", "limit": None},
                "plan": {"enabled": True, "autoPercentUsed": 1.85, "apiPercentUsed": 0},
                "onDemand": {"enabled": True, "used": 0, "limit": 2000, "remaining": 2000},
            }
        }
    ) == [("monthly", "On-Demand Usage", 0.0)]


def test_cursor_fetch_uncapped_overall_hides_chip(monkeypatch):
    token = _jwt({"sub": "auth0|u1"})
    summary = {
        "individualUsage": {
            "overall": {"enabled": True, "used": "2500", "limit": None},
            "plan": {"enabled": True, "autoPercentUsed": 1.85, "apiPercentUsed": 0},
        }
    }
    _patch_client(
        monkeypatch,
        [
            ("api2.cursor.sh/auth/usage", {"planUsage": {"used": 10, "limit": 100}}),
            ("cursor.com/api/usage-summary", summary),
        ],
    )

    assert usage_cursor.fetch_cursor_account_usage(api_key=token) is None
