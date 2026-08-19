"""Tests for the SuperGrok / xAI OAuth account-usage path.

Pure ``unittest`` so the file runs with ``python -m unittest``. Third-party
dependencies are stubbed ONLY when they are not installed. ``httpx`` is
stubbed with a recording fake so the real fetch/parse code runs against
controlled billing payloads.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _install_stub_if_missing(name: str, attrs: dict) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except ImportError:
        pass
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


class _FakeHTTPStatusError(Exception):
    def __init__(self, message, *, request, response):
        super().__init__(message)
        self.request = request
        self.response = response


class _FakeHTTPRequest:
    def __init__(self, method, url):
        self.method = method
        self.url = url


class _FakeHTTPResponse:
    def __init__(self, payload=None, status_code=200, json_error=None):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error_cls = account_usage.httpx.HTTPStatusError
            request = account_usage.httpx.Request("GET", "http://fake.invalid")
            raise error_cls(
                f"HTTP status {self.status_code}",
                request=request,
                response=self,
            )


class _FakeHTTPClient:
    instances: list["_FakeHTTPClient"] = []
    response_queue: list = []

    def __init__(self, timeout=None, **kwargs):
        self.timeout = timeout
        self.requests: list[tuple[str, str, dict]] = []
        _FakeHTTPClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, headers=None):
        self.requests.append(("GET", str(url), dict(headers or {})))
        if _FakeHTTPClient.response_queue:
            return _FakeHTTPClient.response_queue.pop(0)
        return _FakeHTTPResponse()


_install_stub_if_missing(
    "httpx",
    {
        "Client": _FakeHTTPClient,
        "HTTPStatusError": _FakeHTTPStatusError,
        "Request": _FakeHTTPRequest,
        "RequestError": type("RequestError", (Exception,), {}),
    },
)
_install_stub_if_missing(
    "yaml",
    {
        "load": lambda *_a, **_k: {},
        "safe_load": lambda *_a, **_k: {},
        "SafeLoader": object,
        "SafeDumper": object,
        "CSafeLoader": object,
    },
)
_install_stub_if_missing("jiter", {})
_install_stub_if_missing("dotenv", {"load_dotenv": lambda *_a, **_k: False})

from agent import account_usage  # noqa: E402


WEEKLY_FIXTURE = {
    "config": {
        "currentPeriod": {
            "start": "2026-08-11T00:00:00Z",
            "end": "2026-08-18T00:00:00Z",
            "type": "WEEK",
        },
        "creditUsagePercent": 24.0,
        "productUsage": [{"product": "Grok", "usagePercent": 10.0}],
        "isUnifiedBillingUser": False,
    }
}

MONTHLY_FIXTURE = {
    "config": {
        "billingPeriodStart": "2026-08-01T00:00:00Z",
        "billingPeriodEnd": "2026-09-01T00:00:00Z",
        "monthlyLimit": {"val": 1000},
        "used": {"val": 250},
        "isUnifiedBillingUser": True,
    }
}

_TOKEN = "xai-oauth-test-token"


class _FakePooledCredential:
    @classmethod
    def from_dict(cls, provider, row):
        return SimpleNamespace(
            provider=provider,
            id=row["id"],
            label=row.get("label", ""),
            priority=row["priority"],
            runtime_api_key=row.get("access_token", ""),
            runtime_base_url=row.get("base_url", ""),
        )


@contextlib.contextmanager
def _fake_http(*responses):
    _FakeHTTPClient.response_queue = list(responses)
    try:
        with mock.patch.object(account_usage.httpx, "Client", _FakeHTTPClient):
            yield
    finally:
        _FakeHTTPClient.response_queue = []


def _last_client() -> _FakeHTTPClient:
    return _FakeHTTPClient.instances[-1]


class XaiOauthFetchParseTests(unittest.TestCase):
    def setUp(self):
        _FakeHTTPClient.instances.clear()
        _FakeHTTPClient.response_queue = []
        account_usage._clear_pool_account_usage_cache_for_tests()

    def test_weekly_fixture_parses_windows_and_product(self):
        with _fake_http(_FakeHTTPResponse(WEEKLY_FIXTURE)):
            snapshot = account_usage._fetch_xai_oauth_account_usage_with_credentials(_TOKEN)
        self.assertEqual(snapshot.provider, "xai-oauth")
        self.assertEqual(snapshot.source, "usage_api")
        self.assertTrue(snapshot.available)
        self.assertIsNone(snapshot.unavailable_reason)
        labels = [window.label for window in snapshot.windows]
        self.assertEqual(labels, ["Weekly", "Grok (Weekly)"])
        self.assertEqual(snapshot.windows[0].used_percent, 24.0)
        self.assertEqual(snapshot.windows[1].used_percent, 10.0)
        self.assertEqual(
            snapshot.windows[0].reset_at,
            datetime(2026, 8, 18, tzinfo=timezone.utc),
        )

    def test_monthly_probe_when_weekly_missing(self):
        with _fake_http(_FakeHTTPResponse({"config": {}}), _FakeHTTPResponse(MONTHLY_FIXTURE)):
            snapshot = account_usage._fetch_xai_oauth_account_usage_with_credentials(_TOKEN)
        self.assertTrue(snapshot.available)
        self.assertEqual([window.label for window in snapshot.windows], ["Monthly"])
        self.assertEqual(snapshot.windows[0].used_percent, 25.0)

    def test_malformed_body_is_unavailable(self):
        with _fake_http(_FakeHTTPResponse({"nope": True})):
            snapshot = account_usage._fetch_xai_oauth_account_usage_with_credentials(_TOKEN)
        self.assertFalse(snapshot.available)
        self.assertIn("unexpected", snapshot.unavailable_reason or "")

    def test_401_raises_http_status_error(self):
        with _fake_http(_FakeHTTPResponse(status_code=401)):
            with self.assertRaises(account_usage.httpx.HTTPStatusError):
                account_usage._fetch_xai_oauth_account_usage_with_credentials(_TOKEN)

    def test_headers_and_url_and_timeout(self):
        with _fake_http(_FakeHTTPResponse(WEEKLY_FIXTURE)):
            account_usage._fetch_xai_oauth_account_usage_with_credentials(_TOKEN)
        client = _last_client()
        self.assertEqual(client.timeout, account_usage._USAGE_FETCH_TIMEOUT_SECONDS)
        method, url, headers = client.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, account_usage._XAI_OAUTH_BILLING_CREDITS_URL)
        self.assertEqual(headers["Authorization"], f"Bearer {_TOKEN}")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["X-XAI-Token-Auth"], "xai-grok-cli")
        self.assertNotIn(_TOKEN, url)

    def test_fetch_account_usage_dispatches(self):
        with _fake_http(_FakeHTTPResponse(WEEKLY_FIXTURE)):
            snapshot = account_usage.fetch_account_usage("xai-oauth", api_key=_TOKEN)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.provider, "xai-oauth")
        self.assertEqual(snapshot.windows[0].used_percent, 24.0)


class XaiOauthPoolTests(unittest.TestCase):
    def setUp(self):
        _FakeHTTPClient.instances.clear()
        _FakeHTTPClient.response_queue = []
        account_usage._clear_pool_account_usage_cache_for_tests()
        self.fake_pool_module = types.ModuleType("agent.credential_pool")
        self.fake_pool_module.PooledCredential = _FakePooledCredential

    def _rows(self):
        return [
            {
                "id": "entry-a",
                "priority": 0,
                "label": "person@example.com",
                "access_token": _TOKEN,
            },
            {
                "id": "entry-b",
                "priority": 1,
                "label": "backup",
                "access_token": "xai-oauth-backup-token",
            },
        ]

    def _run_pool(self, rows, *, active_entry_id=None, fresh=True, responses=None):
        queued = responses or [_FakeHTTPResponse(WEEKLY_FIXTURE), _FakeHTTPResponse(WEEKLY_FIXTURE)]
        with (
            mock.patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            mock.patch.object(account_usage, "read_credential_pool", return_value=rows),
            _fake_http(*queued),
        ):
            return account_usage.fetch_pool_account_usage(
                "xai-oauth", active_entry_id=active_entry_id, fresh=fresh
            )

    def test_pool_rows_label_and_active_marking(self):
        snapshots = self._run_pool(self._rows(), active_entry_id="entry-b")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual([item.account_label for item in snapshots], ["xAI 1", "xAI 2"])
        self.assertEqual([item.active for item in snapshots], [False, True])
        self.assertEqual(snapshots[0].provider, "xai-oauth")
        payloads = [account_usage.account_usage_snapshot_to_dict(item) for item in snapshots]
        rendered = repr(payloads)
        self.assertNotIn(_TOKEN, rendered)
        self.assertNotIn("credential_id", rendered)
        self.assertNotIn("person@example.com", rendered)
        self.assertNotIn("entry-a", rendered)

    def test_cache_only_skips_network(self):
        self._run_pool(self._rows(), fresh=True)
        first_count = sum(len(client.requests) for client in _FakeHTTPClient.instances)
        cached = self._run_pool(self._rows(), fresh=False, responses=[])
        self.assertEqual(len(cached), 2)
        second_count = sum(len(client.requests) for client in _FakeHTTPClient.instances)
        self.assertEqual(second_count, first_count)

    def test_401_becomes_rejected_unavailable(self):
        snapshots = self._run_pool(
            [self._rows()[0]],
            fresh=True,
            responses=[_FakeHTTPResponse(status_code=401)],
        )
        self.assertEqual(len(snapshots), 1)
        self.assertFalse(snapshots[0].available)
        self.assertIn("rejected", snapshots[0].unavailable_reason or "")

    def test_empty_pool_without_singleton_is_empty(self):
        with (
            mock.patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            mock.patch.object(account_usage, "read_credential_pool", return_value=[]),
            mock.patch.object(
                account_usage,
                "_fetch_xai_oauth_singleton_usage_snapshot",
                return_value=(),
            ),
        ):
            self.assertEqual(account_usage.fetch_pool_account_usage("xai-oauth", fresh=True), ())

    def test_empty_pool_uses_singleton_fallback(self):
        singleton = account_usage.AccountUsageSnapshot(
            provider="xai-oauth",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            windows=(account_usage.AccountUsageWindow("Weekly", 24.0),),
        )
        with (
            mock.patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            mock.patch.object(account_usage, "read_credential_pool", return_value=[]),
            mock.patch.object(
                account_usage,
                "_fetch_xai_oauth_singleton_usage_snapshot",
                return_value=(singleton,),
            ) as fallback,
        ):
            snapshots = account_usage.fetch_pool_account_usage("xai-oauth", fresh=True)
        fallback.assert_called_once_with(fresh=True)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].windows[0].used_percent, 24.0)


class XaiOauthForbiddenSymbolTests(unittest.TestCase):
    FORBIDDEN = {"resolve_runtime_provider", "load_pool", "select", "peek"}

    def test_xai_functions_do_not_call_pool_mutators(self):
        source = inspect.getsource(account_usage)
        tree = ast.parse(source)
        names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        }
        attrs = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }
        xai_fns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and "xai" in node.name
        ]
        self.assertGreater(len(xai_fns), 0)
        used = set()
        for fn in xai_fns:
            used.update(
                n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id in self.FORBIDDEN
            )
            used.update(
                n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr in self.FORBIDDEN
            )
        self.assertFalse(used & self.FORBIDDEN)
        # The module may mention load_pool elsewhere; xAI helpers must not.
        self.assertTrue(names or attrs)


if __name__ == "__main__":
    unittest.main()
