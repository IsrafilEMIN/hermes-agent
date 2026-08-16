"""Tests for the OpenCode Go account-usage path in ``agent/account_usage.py``.

Pure ``unittest`` (no pytest import) so the file runs with plain
``python -m unittest`` and with the repo's offline harness: third-party
dependencies (httpx/yaml/jiter/dotenv) are stubbed ONLY when they are not
installed (CI installs them, so production import behavior is preserved
there). ``httpx`` is stubbed with a functional fake so the real fetch/parse
code runs end-to-end against controlled responses.

Covers: the exact live payload contract, canonical URL / Bearer / timeout,
fixed window order, percent used + clamping + reset parsing, ``status``
ignored, missing/non-dict/malformed payloads, env vs explicit credential
resolution, official ``/zen/go`` canonicalization, custom base preservation,
the forbidden ``resolve_runtime_provider`` / ``load_pool`` / ``select`` /
``peek`` symbols, secret-free snapshots/output, and the ``fetch_pool`` path:
visible manual pool rows (env absent, active marking, per-row failure
isolation, per-entry-id cache, secret safety, forbidden mutation symbols)
and the env-only compatibility fallback when no rows are visible (missing
credential, cache, fresh, failure → unavailable).
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import sys
import threading
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── Offline harness: conditional third-party stubs ──────────────────────────
def _install_stub_if_missing(name: str, attrs: dict) -> None:
    """Inject a minimal module stub only when ``name`` cannot be imported."""
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
    """Stands in for ``httpx.HTTPStatusError`` in the offline stub.

    Mirrors the real httpx constructor signature ``(message, *, request,
    response)`` (stable across httpx 0.21+), so the fake transport can raise
    the exact class the module under test catches in BOTH modes: the real
    ``httpx.HTTPStatusError`` when httpx is installed, and this class (which
    the offline stub registers AS ``httpx.HTTPStatusError``) when it is not.
    """

    def __init__(self, message, *, request, response):
        super().__init__(message)
        self.request = request
        self.response = response


class _FakeHTTPRequest:
    """A minimal ``httpx.Request`` stand-in for the offline stub."""

    def __init__(self, method, url):
        self.method = method
        self.url = url


class _FakeHTTPResponse:
    """A controllable ``httpx.Response`` stand-in."""

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
            # Raise the module-under-test's ACTUAL HTTPStatusError class:
            # ``account_usage.httpx`` is the real httpx module when it is
            # installed (host/CI) and the offline stub module when it is not
            # (container without deps). Looking the class up here instead of
            # raising a bespoke exception keeps exact class identity with the
            # ``except httpx.HTTPStatusError`` handlers in production, so
            # 401/403 → "rejected" classification works in both modes.
            error_cls = account_usage.httpx.HTTPStatusError
            request = account_usage.httpx.Request("GET", "http://fake.invalid")
            raise error_cls(
                f"HTTP status {self.status_code}",
                request=request,
                response=self,
            )


class _FakeHTTPClient:
    """A recording ``httpx.Client`` stand-in used to drive the real fetch code."""

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


# ── The exact live payload contract ─────────────────────────────────────────
# Verified-live shape documented in ``_fetch_opencode_go_account_usage_with_credentials``:
#   {"usage": {monthly|rolling|weekly: {"percent": <used %>, "resetsAt": <ISO-8601>,
#                                       "status": <arbitrary string>}}}
LIVE_FIXTURE = {
    "usage": {
        "monthly": {
            "percent": 61.4,
            "resetsAt": "2026-09-01T00:00:00Z",
            "status": "active",
        },
        "rolling": {
            "percent": 22.0,
            "resetsAt": "2026-08-15T17:30:00Z",
            "status": "ok",
        },
        "weekly": {
            "percent": 38.5,
            "resetsAt": "2026-08-21T00:00:00Z",
            "status": "ok",
        },
    }
}

_TOKEN = "sk-test-opencode-go-token"
_CUSTOM_BASE = "https://proxy.example.com/opencode/v1"


@contextlib.contextmanager
def _fake_http(*responses):
    """Patch ``httpx.Client`` with the recording fake and queue responses."""
    _FakeHTTPClient.response_queue = list(responses)
    try:
        with mock.patch.object(account_usage.httpx, "Client", _FakeHTTPClient):
            yield
    finally:
        _FakeHTTPClient.response_queue = []


def _last_client() -> _FakeHTTPClient:
    return _FakeHTTPClient.instances[-1]


class OpenCodeGoCanonicalBaseUrlTests(unittest.TestCase):
    """``_canonical_opencode_go_base_url``: official variants normalize."""

    def test_official_variants_normalize_to_v1(self):
        canonical = account_usage._canonical_opencode_go_base_url
        for variant in (
            None,
            "",
            "   ",
            "https://opencode.ai/zen/go",
            "https://opencode.ai/zen/go/",
            "https://opencode.ai/zen/go/v1",
            "https://opencode.ai/zen/go/v1/",
            "https://OPENCODE.AI/zen/go",
            "https://opencode.ai/zen/go/v1////",
            # The official-host check is netloc/path based: the scheme is not
            # part of the contract, so even an http:// official-host variant
            # normalizes to the canonical https base.
            "http://opencode.ai/zen/go",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(
                    canonical(variant),
                    account_usage._OPENCODE_GO_DEFAULT_BASE_URL,
                )

    def test_custom_bases_are_preserved_untouched(self):
        canonical = account_usage._canonical_opencode_go_base_url
        for custom in (
            "https://opencode.ai/zen/go/v2",
            "https://opencode.ai/zen/go?region=eu",
            "https://opencode.ai/zen/go#frag",
            "https://opencode.ai/zen/go/v1/extra",
            "https://opencode.ai/other/path",
            "https://proxy.example.com/zen/go",
            _CUSTOM_BASE,
        ):
            with self.subTest(custom=custom):
                self.assertEqual(canonical(custom), custom)

    def test_default_constant_is_the_official_v1_base(self):
        self.assertEqual(
            account_usage._OPENCODE_GO_DEFAULT_BASE_URL,
            "https://opencode.ai/zen/go/v1",
        )


class OpenCodeGoCredentialResolutionTests(unittest.TestCase):
    """``_resolve_opencode_go_usage_credentials``: explicit beats env, then env."""

    def _clear_env(self):
        os.environ.pop("OPENCODE_GO_API_KEY", None)
        os.environ.pop("OPENCODE_GO_BASE_URL", None)

    def test_explicit_credentials_win_over_env(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_API_KEY": "sk-env", "OPENCODE_GO_BASE_URL": "https://env.example/v1"},
        ):
            token, base = account_usage._resolve_opencode_go_usage_credentials(
                "https://opencode.ai/zen/go", "sk-explicit"
            )
        self.assertEqual(token, "sk-explicit")
        self.assertEqual(base, account_usage._OPENCODE_GO_DEFAULT_BASE_URL)

    def test_explicit_key_with_env_base(self):
        with mock.patch.dict(os.environ, {"OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go"}):
            token, base = account_usage._resolve_opencode_go_usage_credentials(None, "sk-explicit")
        self.assertEqual(token, "sk-explicit")
        self.assertEqual(base, account_usage._OPENCODE_GO_DEFAULT_BASE_URL)

    def test_env_fallback_used_when_no_explicit(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_API_KEY": "sk-env-key", "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go"},
        ):
            token, base = account_usage._resolve_opencode_go_usage_credentials(None, None)
        self.assertEqual(token, "sk-env-key")
        self.assertEqual(base, account_usage._OPENCODE_GO_DEFAULT_BASE_URL)

    def test_env_fallback_canonicalizes_env_base(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_API_KEY": "sk-env-key", "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go"},
        ):
            token, base = account_usage._resolve_opencode_go_usage_credentials("", "")
        self.assertEqual(token, "sk-env-key")
        self.assertEqual(base, account_usage._OPENCODE_GO_DEFAULT_BASE_URL)

    def test_env_base_preserves_custom_base(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_API_KEY": "sk-env-key", "OPENCODE_GO_BASE_URL": _CUSTOM_BASE},
        ):
            token, base = account_usage._resolve_opencode_go_usage_credentials(None, None)
        self.assertEqual((token, base), ("sk-env-key", _CUSTOM_BASE))

    def test_no_credentials_raises_with_guidance(self):
        with mock.patch.dict(os.environ):
            self._clear_env()
            with self.assertRaisesRegex(RuntimeError, "OPENCODE_GO_API_KEY"):
                account_usage._resolve_opencode_go_usage_credentials(None, None)

    def test_whitespace_only_values_count_as_missing(self):
        with mock.patch.dict(os.environ):
            self._clear_env()
            with self.assertRaisesRegex(RuntimeError, "OPENCODE_GO_API_KEY"):
                account_usage._resolve_opencode_go_usage_credentials("   ", "   ")
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_API_KEY": "sk-env-key", "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go"},
        ):
            token, base = account_usage._resolve_opencode_go_usage_credentials("  ", "  ")
        self.assertEqual((token, base), ("sk-env-key", account_usage._OPENCODE_GO_DEFAULT_BASE_URL))


class OpenCodeGoFetchParseTests(unittest.TestCase):
    """``_fetch_opencode_go_account_usage_with_credentials``: transport + parse."""

    def setUp(self):
        _FakeHTTPClient.instances.clear()
        _FakeHTTPClient.response_queue = []

    def test_live_fixture_parses_to_fixed_window_order(self):
        with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        self.assertEqual(snapshot.provider, "opencode-go")
        self.assertEqual(snapshot.source, "usage_api")
        self.assertTrue(snapshot.available)
        self.assertIsNone(snapshot.unavailable_reason)
        # Fixed display order: rolling → weekly → monthly, regardless of the
        # order the backend emits the keys in (the fixture lists monthly first).
        self.assertEqual(
            [window.label for window in snapshot.windows],
            ["Rolling 5h", "Weekly", "Monthly"],
        )
        self.assertEqual(
            [window.used_percent for window in snapshot.windows],
            [22.0, 38.5, 61.4],
        )
        self.assertEqual(
            [window.reset_at for window in snapshot.windows],
            [
                datetime(2026, 8, 15, 17, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc),
            ],
        )

    def test_canonical_url_bearer_accept_and_timeout(self):
        with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
            account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, "https://opencode.ai/zen/go"
            )
        client = _last_client()
        # Quota telemetry is cosmetic: the transport gives up after 4s so a
        # slow/unreachable usage endpoint can never stall the status bar (or
        # the end-of-turn refresh) for a quarter of a minute.
        self.assertEqual(client.timeout, 4.0)
        method, url, headers = client.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://opencode.ai/zen/go/v1/usage")
        self.assertEqual(headers["Authorization"], f"Bearer {_TOKEN}")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["User-Agent"], "hermes")

    def test_explicit_official_variant_hits_canonical_url(self):
        with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
            account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, "https://opencode.ai/zen/go"
            )
        self.assertEqual(_last_client().requests[0][1], "https://opencode.ai/zen/go/v1/usage")

    def test_custom_base_is_preserved_on_fetch(self):
        with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
            account_usage._fetch_opencode_go_account_usage_with_credentials(_TOKEN, _CUSTOM_BASE)
        self.assertEqual(_last_client().requests[0][1], f"{_CUSTOM_BASE}/usage")

    def test_resolution_then_fetch_uses_canonical_url(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go"},
        ):
            with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
                snapshot = account_usage._fetch_opencode_go_account_usage()
        self.assertTrue(snapshot.available)
        self.assertEqual(_last_client().requests[0][1], "https://opencode.ai/zen/go/v1/usage")

    def test_percent_used_is_clamped_to_0_100(self):
        payload = {
            "usage": {
                "rolling": {"percent": -5.0, "resetsAt": "2026-08-15T00:00:00Z", "status": "x"},
                "weekly": {"percent": 150.0, "resetsAt": "2026-08-21T00:00:00Z", "status": "x"},
                "monthly": {"percent": 12.5, "resetsAt": "2026-09-01T00:00:00Z", "status": "x"},
            }
        }
        with _fake_http(_FakeHTTPResponse(payload)):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        self.assertEqual(
            [window.used_percent for window in snapshot.windows], [0.0, 100.0, 12.5]
        )

    def test_status_field_is_ignored(self):
        payload = {
            "usage": {
                "rolling": {
                    "percent": 22.0,
                    "resetsAt": "2026-08-15T17:30:00Z",
                    "status": "weird status — never render this",
                }
            }
        }
        with _fake_http(_FakeHTTPResponse(payload)):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        self.assertEqual(len(snapshot.windows), 1)
        self.assertIsNone(snapshot.windows[0].detail)
        self.assertEqual(snapshot.details, ())
        rendered = "\n".join(account_usage.render_account_usage_lines(snapshot))
        self.assertNotIn("weird status", rendered)

    def test_non_dict_payload_yields_unavailable(self):
        for raw in ([], "not-json", 42, None):
            with self.subTest(raw=raw):
                with _fake_http(_FakeHTTPResponse(raw)):
                    snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                        _TOKEN, None
                    )
                self.assertFalse(snapshot.available)
                self.assertEqual(
                    snapshot.unavailable_reason,
                    "The OpenCode Go usage service returned an unexpected response.",
                )
                self.assertEqual(snapshot.windows, ())

    def test_unparseable_json_body_yields_unavailable(self):
        with _fake_http(_FakeHTTPResponse(json_error=ValueError("synthetic bad json"))):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        self.assertFalse(snapshot.available)
        self.assertEqual(
            snapshot.unavailable_reason,
            "The OpenCode Go usage service returned an unexpected response.",
        )

    def test_missing_or_wrong_typed_usage_is_empty_windows_not_unavailable(self):
        for raw in ({}, {"usage": "oops"}, {"usage": None}, {"usage": []}, {"other": 1}):
            with self.subTest(raw=raw):
                with _fake_http(_FakeHTTPResponse(raw)):
                    snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                        _TOKEN, None
                    )
                self.assertEqual(snapshot.windows, ())
                self.assertIsNone(snapshot.unavailable_reason)
                self.assertFalse(snapshot.available)

    def test_missing_or_non_numeric_percent_skips_window(self):
        payload = {
            "usage": {
                "rolling": {"resetsAt": "2026-08-15T17:30:00Z", "status": "ok"},
                "weekly": {"percent": "38.5", "resetsAt": "2026-08-21T00:00:00Z", "status": "ok"},
                "monthly": {"percent": None, "resetsAt": "2026-09-01T00:00:00Z", "status": "ok"},
            }
        }
        with _fake_http(_FakeHTTPResponse(payload)):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        self.assertEqual(snapshot.windows, ())
        # A bool is not a finite number: skipped too.
        payload["usage"]["rolling"] = {"percent": True, "resetsAt": "2026-08-15T17:30:00Z"}
        with _fake_http(_FakeHTTPResponse(payload)):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        self.assertEqual(snapshot.windows, ())

    def test_malformed_resets_at_parses_to_none(self):
        payload = {
            "usage": {
                "rolling": {"percent": 22.0, "resetsAt": "not-a-date", "status": "ok"},
                "weekly": {"percent": 38.5, "resetsAt": "", "status": "ok"},
                "monthly": {"percent": 61.4, "resetsAt": 1755000000, "status": "ok"},
            }
        }
        with _fake_http(_FakeHTTPResponse(payload)):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        self.assertEqual(len(snapshot.windows), 3)
        self.assertIsNone(snapshot.windows[0].reset_at)
        self.assertIsNone(snapshot.windows[1].reset_at)
        # Numeric epoch seconds parse.
        self.assertEqual(
            snapshot.windows[2].reset_at,
            datetime.fromtimestamp(1755000000, tz=timezone.utc),
        )

    def test_window_specs_constant_is_fixed_and_ordered(self):
        self.assertEqual(
            account_usage._OPENCODE_GO_WINDOW_SPECS,
            (("rolling", "Rolling 5h"), ("weekly", "Weekly"), ("monthly", "Monthly")),
        )


class OpenCodeGoForbiddenSymbolTests(unittest.TestCase):
    """The opencode-go path must never use pool/runtime-provider machinery."""

    FORBIDDEN = {"resolve_runtime_provider", "load_pool", "select", "peek"}
    GO_FUNCTIONS = (
        account_usage._canonical_opencode_go_base_url,
        account_usage._resolve_opencode_go_usage_credentials,
        account_usage._fetch_opencode_go_account_usage_with_credentials,
        account_usage._fetch_opencode_go_account_usage,
        account_usage._fetch_opencode_go_env_usage_snapshot,
        account_usage._hydrate_opencode_go_env_rows,
        account_usage._fetch_opencode_go_pool_usage_snapshots,
    )

    @staticmethod
    def _used_identifiers(func) -> set[str]:
        """Identifiers actually referenced in the function body (no docstrings)."""
        tree = ast.parse(inspect.getsource(func))
        body = tree.body[0].body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        names: set[str] = set()
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    def test_go_functions_never_reference_forbidden_symbols(self):
        for func in self.GO_FUNCTIONS:
            used = self._used_identifiers(func)
            hit = used & self.FORBIDDEN
            self.assertEqual(hit, set(), f"{func.__name__} references forbidden symbols: {hit}")

    def test_go_fetch_never_calls_runtime_provider(self):
        with mock.patch.object(
            account_usage, "resolve_runtime_provider", mock.Mock(return_value={})
        ) as runtime_mock:
            with mock.patch.dict(
                os.environ,
                {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go"},
            ):
                with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
                    snapshot = account_usage._fetch_opencode_go_account_usage()
        self.assertTrue(snapshot.available)
        runtime_mock.assert_not_called()

    def test_pool_path_never_loads_selects_or_peeks(self):
        poisoned = types.ModuleType("agent.credential_pool")

        def _forbidden(*_a, **_k):
            raise AssertionError("forbidden credential-pool symbol used by opencode-go path")

        poisoned.load_pool = _forbidden
        poisoned.select = _forbidden
        poisoned.peek = _forbidden
        with mock.patch.dict(sys.modules, {"agent.credential_pool": poisoned}):
            with mock.patch.dict(
                os.environ,
                {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE},
            ):
                account_usage._clear_pool_account_usage_cache_for_tests()
                with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
                    snapshots = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].available)


class OpenCodeGoSecretSafetyTests(unittest.TestCase):
    """Tokens and internal ids must never appear in snapshots or rendered output."""

    def setUp(self):
        _FakeHTTPClient.instances.clear()
        _FakeHTTPClient.response_queue = []

    def test_snapshot_dict_and_lines_never_contain_token(self):
        with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
            snapshot = account_usage._fetch_opencode_go_account_usage_with_credentials(
                _TOKEN, None
            )
        payload = account_usage.account_usage_snapshot_to_dict(snapshot)
        rendered = repr(payload) + "\n" + "\n".join(
            account_usage.render_account_usage_lines(snapshot)
        )
        self.assertNotIn(_TOKEN, rendered)
        # The Bearer did go on the wire (the credential is used, not leaked).
        _, _, headers = _last_client().requests[0]
        self.assertEqual(headers["Authorization"], f"Bearer {_TOKEN}")

    def test_fetch_pool_env_snapshot_is_secret_safe(self):
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            account_usage._clear_pool_account_usage_cache_for_tests()
            with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
                snapshots = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
        payloads = [account_usage.account_usage_snapshot_to_dict(item) for item in snapshots]
        rendered = repr(payloads)
        self.assertNotIn(_TOKEN, rendered)
        self.assertNotIn("credential_id", rendered)
        self.assertEqual(payloads[0]["provider"], "opencode-go")
        self.assertTrue(payloads[0]["active"])

    def test_unavailable_reasons_never_contain_token_or_credential_id(self):
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            account_usage._clear_pool_account_usage_cache_for_tests()
            with _fake_http(_FakeHTTPResponse(status_code=401)):
                snapshots = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
        rendered = repr([account_usage.account_usage_snapshot_to_dict(item) for item in snapshots])
        self.assertNotIn(_TOKEN, rendered)
        self.assertNotIn("credential_id", rendered)

    def test_serializer_omits_internal_credential_id(self):
        snapshot = account_usage.AccountUsageSnapshot(
            provider="opencode-go",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            credential_id="env-cred-id",
            account_label="Go",
            active=True,
            windows=(account_usage.AccountUsageWindow("Rolling 5h", used_percent=22.0),),
        )
        payload = account_usage.account_usage_snapshot_to_dict(snapshot)
        rendered = repr(payload)
        self.assertNotIn("env-cred-id", rendered)
        self.assertNotIn("credential_id", rendered)
        self.assertEqual(payload["label"], "Go")
        self.assertTrue(payload["active"])
        self.assertEqual(payload["windows"][0]["used_percent"], 22.0)
        self.assertIsNone(payload["windows"][0]["reset_at"])
        self.assertIsNone(payload["windows"][0]["reset_human"])


class OpenCodeGoFetchPoolEnvPathTests(unittest.TestCase):
    """``fetch_pool_account_usage("opencode-go")`` env path: missing/cache/fresh/failure.

    Hermetic by construction: the pool read is stubbed EMPTY so the env-only
    fallback is exercised regardless of the host's real auth.json (a real
    opencode-go pool row on the developer machine would otherwise route
    these tests into the pool path — credential_id appears, singleton shape
    breaks).
    """

    def setUp(self):
        _FakeHTTPClient.instances.clear()
        _FakeHTTPClient.response_queue = []
        account_usage._clear_pool_account_usage_cache_for_tests()
        pool_empty = mock.patch.object(account_usage, "read_credential_pool", return_value=[])
        pool_empty.start()
        self.addCleanup(pool_empty.stop)

    def test_no_env_credential_is_empty_pool(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("OPENCODE_GO_API_KEY", None)
            os.environ.pop("OPENCODE_GO_BASE_URL", None)
            self.assertEqual(account_usage.fetch_pool_account_usage("opencode-go"), ())
            self.assertEqual(account_usage.fetch_pool_account_usage("opencode-go", fresh=True), ())

    def test_env_snapshot_is_active_and_singleton(self):
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
                snapshots = account_usage.fetch_pool_account_usage(
                    "opencode-go", active_entry_id="whatever", fresh=True
                )
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].active)
        self.assertEqual(snapshots[0].provider, "opencode-go")
        self.assertIsNone(snapshots[0].credential_id)
        self.assertEqual(len(_last_client().requests), 1)

    def test_cached_then_fresh_then_cached_again(self):
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            with _fake_http(
                _FakeHTTPResponse(LIVE_FIXTURE),
                _FakeHTTPResponse(
                    {
                        "usage": {
                            "rolling": {"percent": 77.0, "resetsAt": "2026-08-15T17:30:00Z"},
                            "weekly": {"percent": 55.0, "resetsAt": "2026-08-21T00:00:00Z"},
                            "monthly": {"percent": 88.0, "resetsAt": "2026-09-01T00:00:00Z"},
                        }
                    }
                ),
            ):
                first = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
                cached = account_usage.fetch_pool_account_usage("opencode-go")
                refreshed = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
                after = account_usage.fetch_pool_account_usage("opencode-go")
        total_requests = sum(len(client.requests) for client in _FakeHTTPClient.instances)
        self.assertEqual(total_requests, 2, "cached calls must not refetch")
        self.assertEqual(first[0].windows[0].used_percent, 22.0)
        self.assertEqual(cached[0].windows[0].used_percent, 22.0)
        self.assertEqual(refreshed[0].windows[0].used_percent, 77.0)
        # The fresh result replaced the cache entry.
        self.assertEqual(after[0].windows[0].used_percent, 77.0)
        for item in (first[0], cached[0], refreshed[0], after[0]):
            self.assertTrue(item.active)

    def test_expired_cache_entry_is_not_refetched_by_default(self):
        """Cache-only contract: an EXPIRED cache entry yields nothing on a
        fresh=False call (no network); only fresh=True refetches."""
        clock = {"now": 1000.0}

        def fake_monotonic():
            return clock["now"]

        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            with mock.patch.object(account_usage.time, "monotonic", fake_monotonic):
                with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE), _FakeHTTPResponse(LIVE_FIXTURE)):
                    account_usage.fetch_pool_account_usage("opencode-go", fresh=True)  # fetch #1, cached at t=1000
                    clock["now"] = 1000.0 + account_usage._POOL_USAGE_CACHE_TTL_SECONDS + 1.0
                    # Expired + cache-only default: nothing, no network.
                    self.assertEqual(
                        account_usage.fetch_pool_account_usage("opencode-go"), ()
                    )
                    account_usage.fetch_pool_account_usage("opencode-go", fresh=True)  # fetch #2
        total_requests = sum(len(client.requests) for client in _FakeHTTPClient.instances)
        self.assertEqual(total_requests, 2)

    def test_http_401_403_map_to_rejected_reason(self):
        for status in (401, 403):
            with self.subTest(status=status):
                with mock.patch.dict(
                    os.environ,
                    {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE},
                ):
                    account_usage._clear_pool_account_usage_cache_for_tests()
                    with _fake_http(_FakeHTTPResponse(status_code=status)):
                        snapshots = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
                self.assertEqual(len(snapshots), 1)
                self.assertFalse(snapshots[0].available)
                self.assertEqual(
                    snapshots[0].unavailable_reason,
                    "The stored OpenCode Go API key was rejected.",
                )
                self.assertTrue(snapshots[0].active)

    def test_other_http_error_maps_to_unavailable(self):
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            with _fake_http(_FakeHTTPResponse(status_code=500)):
                snapshots = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
        self.assertFalse(snapshots[0].available)
        self.assertEqual(
            snapshots[0].unavailable_reason,
            "The OpenCode Go usage service is temporarily unavailable.",
        )

    def test_transport_error_maps_to_unavailable(self):
        def _boom(*_a, **_k):
            raise RuntimeError("synthetic transport failure")

        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            with mock.patch.object(account_usage.httpx, "Client", _boom):
                snapshots = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
        self.assertFalse(snapshots[0].available)
        self.assertEqual(
            snapshots[0].unavailable_reason,
            "The OpenCode Go usage service is temporarily unavailable.",
        )
        self.assertNotIn(_TOKEN, repr(snapshots))

    def test_non_opencode_providers_are_not_enumerated(self):
        self.assertEqual(account_usage.fetch_pool_account_usage("anthropic"), ())
        self.assertEqual(account_usage.fetch_pool_account_usage("nous"), ())
        self.assertEqual(account_usage.fetch_pool_account_usage(""), ())

    def test_fetch_account_usage_dispatch(self):
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            with _fake_http(_FakeHTTPResponse(LIVE_FIXTURE)):
                snapshot = account_usage.fetch_account_usage("opencode-go")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.provider, "opencode-go")
        self.assertEqual(snapshot.windows[0].used_percent, 22.0)

    def test_fetch_account_usage_fail_open_without_env(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("OPENCODE_GO_API_KEY", None)
            os.environ.pop("OPENCODE_GO_BASE_URL", None)
            self.assertIsNone(account_usage.fetch_account_usage("opencode-go"))
            self.assertIsNone(account_usage.fetch_account_usage("auto"))
            self.assertIsNone(account_usage.fetch_account_usage(None))


class _FakePooledCredential:
    """Mirror of the real ``PooledCredential`` surface the pool path touches.

    The pool path only ever reads ``id``, ``priority``, ``source``,
    ``runtime_api_key``, and ``runtime_base_url`` after ``from_dict``, and
    (since Fix A) writes ``access_token`` when hydrating borrowed ``env:``
    rows from the live ``get_env_prefer_dotenv`` value.  The fake carries
    ``access_token``/``source`` as writable attrs and exposes
    ``runtime_api_key``/``runtime_base_url`` as properties — exactly like the
    real dataclass — so hydration is exercised end-to-end.
    """

    def __init__(self, *, id, priority, source, access_token, base_url):
        self.id = id
        self.priority = priority
        self.source = source
        self.access_token = access_token
        self.base_url = base_url

    @property
    def runtime_api_key(self) -> str:
        return str(self.access_token or "")

    @property
    def runtime_base_url(self) -> str:
        return str(self.base_url or "")

    @classmethod
    def from_dict(cls, provider, row):
        return cls(
            id=row["id"],
            priority=row["priority"],
            source=row.get("source", ""),
            access_token=row.get("access_token", ""),
            base_url=row.get("base_url", ""),
        )


class OpenCodeGoPoolRowsTests(unittest.TestCase):
    """``fetch_pool_account_usage("opencode-go")`` with visible manual rows.

    Mirrors the Codex pool-row tests: enumeration via raw
    ``read_credential_pool`` + ``PooledCredential.from_dict`` (never
    load/select/peek), per-row credentials plumbed through the explicit Go
    transport, per-entry-id cache, independent failure isolation, safe
    ordinal labels, secret-free serialization, and the env-only fallback when
    no rows are visible.
    """

    def setUp(self):
        account_usage._clear_pool_account_usage_cache_for_tests()
        _FakeHTTPClient.instances.clear()
        _FakeHTTPClient.response_queue = []
        self.rows = [
            {
                "id": "entry-a",
                "priority": 0,
                "label": "person@example.com",
                "access_token": "synthetic-token-a",
                "base_url": "https://go-a.example.com/v1",
            },
            {
                "id": "entry-b",
                "priority": 1,
                "label": "OPENCODE_GO_API_KEY",
                "access_token": "synthetic-token-b",
                "base_url": "",
            },
        ]
        self.fake_pool_module = types.ModuleType("agent.credential_pool")
        self.fake_pool_module.PooledCredential = _FakePooledCredential
        # Fix A: the pool path may re-resolve borrowed env: rows from the
        # live environment. Stub the seeder's helper as a pure os.environ
        # read so hydration tests are deterministic.
        self.fake_pool_module.get_env_prefer_dotenv = os.environ.get

    def _run(self, fetcher, *, active_entry_id=None, fresh=True, rows=None, clear_env=True):
        """Run the pool fetch with stubbed rows + transport.

        This class exercises the LIVE fetch path (pool rows, failures, fresh
        semantics): fresh defaults True because the cache-only contract makes
        fresh=False return nothing on a cold cache. Tests that specifically
        probe cached reads pass fresh=False.

        ``clear_env=True`` (default) removes the OpenCode Go env vars so pool
        rows are exercised with env absent; fallback tests pass False to keep
        a configured env credential visible.
        """
        with (
            mock.patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            mock.patch.object(
                account_usage,
                "read_credential_pool",
                return_value=self.rows if rows is None else rows,
            ),
            mock.patch.object(
                account_usage,
                "_fetch_opencode_go_account_usage_with_credentials",
                side_effect=fetcher,
            ),
            mock.patch.dict(os.environ),
        ):
            if clear_env:
                os.environ.pop("OPENCODE_GO_API_KEY", None)
                os.environ.pop("OPENCODE_GO_BASE_URL", None)
            return account_usage.fetch_pool_account_usage(
                "opencode-go",
                active_entry_id=active_entry_id,
                fresh=fresh,
            )

    @staticmethod
    def _snapshot(*, used=22.0, details=("available",)):
        return account_usage.AccountUsageSnapshot(
            provider="opencode-go",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            windows=(account_usage.AccountUsageWindow("Rolling 5h", used_percent=used),),
            details=details,
        )

    def test_manual_rows_used_with_env_absent(self):
        calls = []
        lock = threading.Lock()

        def fetcher(token, base_url):
            with lock:
                calls.append((token, base_url))
            used = 13.0 if token.endswith("a") else 58.0
            return self._snapshot(used=used)

        snapshots = self._run(fetcher, active_entry_id="entry-b")

        # Both rows enumerated; each row's own runtime credentials plumbed
        # through the explicit Go transport, with no env consulted at all.
        self.assertCountEqual(
            calls,
            [
                ("synthetic-token-a", "https://go-a.example.com/v1"),
                ("synthetic-token-b", ""),
            ],
        )
        self.assertEqual(len(snapshots), 2)
        self.assertEqual([item.provider for item in snapshots], ["opencode-go", "opencode-go"])
        # Safe ordinal labels — raw row labels (email / env-var shaped) never
        # surface.
        self.assertEqual([item.account_label for item in snapshots], ["OpenCode Go 1", "OpenCode Go 2"])
        # Active marking follows active_entry_id.
        self.assertEqual([item.active for item in snapshots], [False, True])
        self.assertEqual([item.windows[0].used_percent for item in snapshots], [13.0, 58.0])
        # Internal correlation ids are attached but never serialized.
        self.assertEqual([item.credential_id for item in snapshots], ["entry-a", "entry-b"])

    def test_active_marking_falls_back_to_first_usable_key(self):
        def fetcher(token, _base_url):
            return self._snapshot(used=float(len(token)))

        # No active_entry_id → first row (priority order) with a usable key.
        snapshots = self._run(fetcher, active_entry_id=None)
        self.assertEqual([item.active for item in snapshots], [True, False])
        # Unknown active_entry_id → same fallback.
        snapshots = self._run(fetcher, active_entry_id="not-a-row")
        self.assertEqual([item.active for item in snapshots], [True, False])

    def test_row_without_key_is_unavailable_and_never_fetched(self):
        self.rows[1]["access_token"] = ""
        calls = []

        def fetcher(token, _base_url):
            calls.append(token)
            return self._snapshot()

        snapshots = self._run(fetcher, active_entry_id="entry-b")
        self.assertEqual(calls, ["synthetic-token-a"])
        self.assertFalse(snapshots[1].available)
        self.assertEqual(
            snapshots[1].unavailable_reason,
            "No usable OpenCode Go API key is stored for this account.",
        )
        # The keyless row can still be marked active (it is the live agent's
        # row; the display just shows it as unavailable).
        self.assertTrue(snapshots[1].active)

    def test_env_row_hydrates_token_from_live_env(self):
        # Fix A: a borrowed env: row (source "env:OPENCODE_GO_API_KEY",
        # persisted tokenless on disk) must be re-resolved from the live
        # environment so env-backed profiles render real numbers instead of
        # "No usable OpenCode Go API key is stored for this account."
        self.rows = [
            {
                "id": "env-row",
                "priority": 0,
                "label": "OPENCODE_GO_API_KEY",
                "source": "env:OPENCODE_GO_API_KEY",
                "access_token": "",
                "base_url": "",
            }
        ]
        calls = []

        def fetcher(token, base_url):
            calls.append((token, base_url))
            return self._snapshot(used=42.0)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": _TOKEN}):
            snapshots = self._run(fetcher, rows=self.rows, clear_env=False)

        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].available)
        self.assertIsNone(snapshots[0].unavailable_reason)
        self.assertEqual(snapshots[0].windows[0].used_percent, 42.0)
        # The token actually used is the live env value, not the empty row.
        self.assertEqual(calls, [(_TOKEN, "")])
        self.assertEqual(snapshots[0].account_label, "OpenCode Go 1")
        self.assertTrue(snapshots[0].active)

    def test_env_row_without_env_value_stays_unavailable(self):
        # Same borrowed env: row, but OPENCODE_GO_API_KEY is NOT set:
        # hydration yields nothing, the row is never fetched, and it renders
        # as an unavailable account (never crashes, never leaks).
        self.rows = [
            {
                "id": "env-row",
                "priority": 0,
                "label": "OPENCODE_GO_API_KEY",
                "source": "env:OPENCODE_GO_API_KEY",
                "access_token": "",
                "base_url": "",
            }
        ]
        calls = []

        def fetcher(token, base_url):
            calls.append((token, base_url))
            return self._snapshot()

        snapshots = self._run(fetcher, rows=self.rows)  # clear_env=True
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(calls, [])
        self.assertFalse(snapshots[0].available)
        self.assertEqual(
            snapshots[0].unavailable_reason,
            "No usable OpenCode Go API key is stored for this account.",
        )

    def test_env_row_with_empty_env_value_stays_unavailable(self):
        # The var EXISTS but is empty — hydration must treat it like unset:
        # no fetch, unavailable account, no crash.
        self.rows = [
            {
                "id": "env-row",
                "priority": 0,
                "label": "OPENCODE_GO_API_KEY",
                "source": "env:OPENCODE_GO_API_KEY",
                "access_token": "",
                "base_url": "",
            }
        ]
        calls = []

        def fetcher(token, base_url):
            calls.append((token, base_url))
            return self._snapshot()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": ""}):
            snapshots = self._run(fetcher, rows=self.rows, clear_env=False)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(calls, [])
        self.assertFalse(snapshots[0].available)

    def test_mixed_manual_and_env_rows_hydrated_independently(self):
        # One manual row (token on disk) + one borrowed env row (tokenless on
        # disk): the manual row fetches with its stored token, the env row is
        # hydrated from the live var, and both are isolated on failure.
        self.rows = [
            {
                "id": "manual-row",
                "priority": 0,
                "label": "person@example.com",
                "source": "manual",
                "access_token": "synthetic-token-manual",
                "base_url": "",
            },
            {
                "id": "env-row",
                "priority": 1,
                "label": "OPENCODE_GO_API_KEY",
                "source": "env:OPENCODE_GO_API_KEY",
                "access_token": "",
                "base_url": "",
            },
        ]
        calls = []

        def fetcher(token, base_url):
            calls.append((token, base_url))
            return self._snapshot(used=float(len(token)))

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": _TOKEN}):
            snapshots = self._run(fetcher, rows=self.rows, clear_env=False)

        self.assertEqual(len(snapshots), 2)
        # Manual row keeps its stored token; env row used the live value.
        self.assertEqual(
            sorted(calls),
            [(_TOKEN, ""), ("synthetic-token-manual", "")],
        )
        self.assertEqual(
            [item.windows[0].used_percent for item in snapshots],
            [len("synthetic-token-manual"), len(_TOKEN)],
        )
        self.assertEqual([item.account_label for item in snapshots], ["OpenCode Go 1", "OpenCode Go 2"])
        # Active falls to the first row with a usable key (the manual row).
        self.assertEqual([item.active for item in snapshots], [True, False])

    def test_suppressed_env_row_is_not_hydrated(self):
        # The user removed the env source (hermes auth remove opencode-go);
        # the usage path must NOT resurrect it from the live env var.
        self.rows = [
            {
                "id": "env-row",
                "priority": 0,
                "label": "OPENCODE_GO_API_KEY",
                "source": "env:OPENCODE_GO_API_KEY",
                "access_token": "",
                "base_url": "",
            }
        ]
        calls = []

        def fetcher(token, base_url):
            calls.append((token, base_url))
            return self._snapshot()

        with (
            mock.patch.object(
                account_usage, "is_source_suppressed", return_value=True
            ) as suppressed_mock,
            mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": _TOKEN}),
        ):
            snapshots = self._run(fetcher, rows=self.rows, clear_env=False)

        suppressed_mock.assert_called_once_with("opencode-go", "env:OPENCODE_GO_API_KEY")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(calls, [])
        self.assertFalse(snapshots[0].available)
        self.assertEqual(
            snapshots[0].unavailable_reason,
            "No usable OpenCode Go API key is stored for this account.",
        )

    def test_failure_isolation_and_cache_keyed_by_entry_id(self):
        calls = []

        def fetcher(token, _base_url):
            calls.append(token)
            if token.endswith("a"):
                raise RuntimeError("synthetic-token-a must never leak")
            return self._snapshot(used=58.0)

        # One row's transport failure must not hide the other's numbers.
        snapshots = self._run(fetcher, active_entry_id="entry-b")
        self.assertEqual(len(snapshots), 2)
        self.assertFalse(snapshots[0].available)
        self.assertNotIn("synthetic-token", snapshots[0].unavailable_reason or "")
        self.assertEqual(
            snapshots[0].unavailable_reason,
            "The OpenCode Go usage service is temporarily unavailable.",
        )
        self.assertTrue(snapshots[1].available)
        self.assertEqual(snapshots[1].windows[0].used_percent, 58.0)
        self.assertEqual(len(calls), 2)

        # Cached: a second enumeration (different active marking included)
        # makes NO new transport calls — the cache is keyed by entry id and
        # the active flag is re-applied per call.
        snapshots = self._run(fetcher, active_entry_id="entry-a", fresh=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item.active for item in snapshots], [True, False])

    def test_http_401_maps_to_rejected_reason_per_row(self):
        def fetcher(token, _base_url):
            if token.endswith("a"):
                error_cls = account_usage.httpx.HTTPStatusError
                raise error_cls(
                    "HTTP status 401",
                    request=account_usage.httpx.Request("GET", "http://fake.invalid"),
                    response=_FakeHTTPResponse(status_code=401),
                )
            return self._snapshot(used=58.0)

        snapshots = self._run(fetcher)
        self.assertEqual(
            snapshots[0].unavailable_reason,
            "The stored OpenCode Go API key was rejected.",
        )
        self.assertTrue(snapshots[1].available)

    def test_fresh_bypasses_cache_and_refreshes_every_row(self):
        calls = []

        def fetcher(token, _base_url):
            calls.append(token)
            used = 13.0 if token.endswith("a") else 58.0
            return self._snapshot(used=used)

        first = self._run(fetcher)
        self.assertEqual(len(calls), 2)

        def fresh_fetcher(token, _base_url):
            calls.append("fresh:" + token)
            used = 21.0 if token.endswith("a") else 64.0
            return self._snapshot(used=used)

        refreshed = self._run(fresh_fetcher, fresh=True)
        self.assertEqual(
            [item.windows[0].used_percent for item in refreshed], [21.0, 64.0]
        )
        # The fresh result replaced the cache: a later cached enumeration
        # makes no new calls and reports the new numbers.
        after = self._run(fetcher, fresh=False)
        self.assertEqual(
            [item.windows[0].used_percent for item in after], [21.0, 64.0]
        )
        self.assertEqual(
            calls,
            [
                "synthetic-token-a",
                "synthetic-token-b",
                "fresh:synthetic-token-a",
                "fresh:synthetic-token-b",
            ],
        )

    def test_pool_serialization_is_secret_and_id_safe(self):
        def fetcher(token, _base_url):
            return self._snapshot()

        snapshots = self._run(fetcher)
        payloads = [account_usage.account_usage_snapshot_to_dict(item) for item in snapshots]
        rendered = repr(payloads)
        for secret in (
            "synthetic-token",
            "entry-a",
            "entry-b",
            "person@example.com",
            "OPENCODE_GO_API_KEY",
            "go-a.example.com",
        ):
            self.assertNotIn(secret, rendered)
        self.assertNotIn("credential_id", rendered)
        self.assertEqual([item["label"] for item in payloads], ["OpenCode Go 1", "OpenCode Go 2"])
        self.assertEqual([item["active"] for item in payloads], [True, False])

    def test_env_fallback_when_no_rows_visible(self):
        calls = []

        def fetcher(token, base_url):
            calls.append((token, base_url))
            return self._snapshot()

        # No rows + env configured → the classic env-only path runs exactly
        # as before: one active snapshot resolved from the env credentials.
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE},
        ):
            snapshots = self._run(fetcher, rows=[], clear_env=False)
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].active)
        self.assertIsNone(snapshots[0].credential_id)
        self.assertIsNone(snapshots[0].account_label)
        self.assertEqual(calls, [(_TOKEN, _CUSTOM_BASE)])

    def test_pool_read_failure_falls_back_to_env(self):
        calls = []

        def fetcher(token, base_url):
            calls.append((token, base_url))
            return self._snapshot()

        with (
            mock.patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            mock.patch.object(
                account_usage,
                "read_credential_pool",
                side_effect=RuntimeError("synthetic pool read failure"),
            ),
            mock.patch.object(
                account_usage,
                "_fetch_opencode_go_account_usage_with_credentials",
                side_effect=fetcher,
            ),
            mock.patch.dict(
                os.environ,
                {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE},
            ),
        ):
            snapshots = account_usage.fetch_pool_account_usage("opencode-go", fresh=True)
        # A pool-read hiccup must never hide a configured env account.
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].active)
        self.assertEqual(calls, [(_TOKEN, _CUSTOM_BASE)])

    def test_no_rows_and_no_env_is_empty_pool(self):
        with mock.patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}):
            with mock.patch.object(account_usage, "read_credential_pool", return_value=[]):
                with mock.patch.dict(os.environ):
                    os.environ.pop("OPENCODE_GO_API_KEY", None)
                    os.environ.pop("OPENCODE_GO_BASE_URL", None)
                    self.assertEqual(
                        account_usage.fetch_pool_account_usage("opencode-go"), ()
                    )

    def test_pool_rows_never_touch_load_select_or_peek(self):
        """With rows present, the pool path uses ONLY the raw read + from_dict."""

        def _forbidden(*_a, **_k):
            raise AssertionError("forbidden credential-pool symbol used by opencode-go path")

        poisoned = types.ModuleType("agent.credential_pool")
        poisoned.PooledCredential = _FakePooledCredential
        poisoned.load_pool = _forbidden
        poisoned.select = _forbidden
        poisoned.peek = _forbidden

        def fetcher(token, _base_url):
            return self._snapshot()

        with (
            mock.patch.dict(sys.modules, {"agent.credential_pool": poisoned}),
            mock.patch.object(
                account_usage,
                "read_credential_pool",
                return_value=self.rows,
            ),
            mock.patch.object(
                account_usage,
                "_fetch_opencode_go_account_usage_with_credentials",
                side_effect=fetcher,
            ),
            mock.patch.dict(os.environ),
        ):
            os.environ.pop("OPENCODE_GO_API_KEY", None)
            os.environ.pop("OPENCODE_GO_BASE_URL", None)
            snapshots = account_usage.fetch_pool_account_usage(
                "opencode-go", active_entry_id="entry-b", fresh=True
            )
        self.assertEqual(len(snapshots), 2)
        self.assertEqual([item.active for item in snapshots], [False, True])


class OpenCodeGoPoolCacheOnlyTests(unittest.TestCase):
    """Cache-only default (``fresh=False`` never fetches) for the Go pool.

    A push emission (session.info) must never hit the network: a cold cache
    yields nothing, a warm cache yields cached values, and cache-miss rows
    are skipped while ordinal labels keep their position among ALL visible
    rows (so numbering never shifts when a later fresh fetch fills the
    gaps). ``fresh=True`` (the background refresh / explicit /usage) still
    bypasses the cache and fetches.
    """

    def setUp(self):
        account_usage._clear_pool_account_usage_cache_for_tests()
        _FakeHTTPClient.instances.clear()
        _FakeHTTPClient.response_queue = []
        self.rows = [
            {
                "id": "entry-a",
                "priority": 0,
                "label": "person@example.com",
                "access_token": "synthetic-token-a",
                "base_url": "https://go-a.example.com/v1",
            },
            {
                "id": "entry-b",
                "priority": 1,
                "label": "OPENCODE_GO_API_KEY",
                "access_token": "synthetic-token-b",
                "base_url": "",
            },
        ]
        self.fake_pool_module = types.ModuleType("agent.credential_pool")
        self.fake_pool_module.PooledCredential = _FakePooledCredential

    def _run(self, fetcher, *, active_entry_id=None, fresh=False, rows=None, clear_env=True):
        with (
            mock.patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            mock.patch.object(
                account_usage,
                "read_credential_pool",
                return_value=self.rows if rows is None else rows,
            ),
            mock.patch.object(
                account_usage,
                "_fetch_opencode_go_account_usage_with_credentials",
                side_effect=fetcher,
            ),
            mock.patch.dict(os.environ),
        ):
            if clear_env:
                os.environ.pop("OPENCODE_GO_API_KEY", None)
                os.environ.pop("OPENCODE_GO_BASE_URL", None)
            return account_usage.fetch_pool_account_usage(
                "opencode-go",
                active_entry_id=active_entry_id,
                fresh=fresh,
            )

    @staticmethod
    def _snapshot(*, used=22.0):
        return account_usage.AccountUsageSnapshot(
            provider="opencode-go",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            windows=(account_usage.AccountUsageWindow("Rolling 5h", used_percent=used),),
        )

    def test_default_cold_cache_fetches_nothing(self):
        def boom(*_args, **_kwargs):
            self.fail("cache-only default must never perform a fetch")

        self.assertEqual(self._run(boom), ())

    def test_default_warm_cache_served_without_fetch(self):
        calls = []

        def fetcher(token, _base_url):
            calls.append(token)
            return self._snapshot(used=float(len(token)))

        warmed = self._run(fetcher, fresh=True)
        self.assertEqual(len(calls), 2)
        calls.clear()

        served = self._run(lambda *_a, **_k: self.fail("must not fetch"))
        self.assertEqual([s.credential_id for s in served], ["entry-a", "entry-b"])
        self.assertEqual(
            [s.windows[0].used_percent for s in served],
            [s.windows[0].used_percent for s in warmed],
        )
        self.assertEqual(calls, [])

    def test_fresh_still_fetches_with_warm_cache(self):
        calls = []

        def fetcher(token, _base_url):
            calls.append(token)
            return self._snapshot(used=float(len(token)))

        self._run(fetcher)  # warm the cache
        calls.clear()

        served = self._run(fetcher, fresh=True)
        self.assertEqual(len(calls), 2, "fresh=True must bypass the cache and fetch")
        self.assertEqual([s.credential_id for s in served], ["entry-a", "entry-b"])

    def test_default_skips_missing_rows_keeps_ordinals(self):
        def fetcher(token, _base_url):
            return self._snapshot(used=float(len(token)))

        # Warm ONLY entry-a (its row alone), then enumerate the FULL pool
        # with the cache-only default: entry-b is a cache miss and must be
        # skipped, and the surviving ordinal keeps its position among ALL
        # visible rows.
        warmed = self._run(fetcher, fresh=True, rows=[self.rows[0]])
        self.assertEqual([s.credential_id for s in warmed], ["entry-a"])

        served = self._run(
            lambda *_a, **_k: self.fail("must not fetch on miss"),
        )
        self.assertEqual([s.credential_id for s in served], ["entry-a"])
        self.assertEqual([s.account_label for s in served], ["OpenCode Go 1"])

    def test_env_fallback_default_cold_returns_empty(self):
        def boom(*_args, **_kwargs):
            self.fail("cache-only env fallback must never fetch")

        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            result = self._run(boom, rows=[], clear_env=False)
        self.assertEqual(result, ())

    def test_env_fallback_default_warm_serves_cache(self):
        def fetcher(token, _base_url):
            return self._snapshot(used=33.0)

        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": _TOKEN, "OPENCODE_GO_BASE_URL": _CUSTOM_BASE}
        ):
            warmed = self._run(fetcher, fresh=True, rows=[], clear_env=False)
            self.assertEqual(len(warmed), 1)

            served = self._run(
                lambda *_a, **_k: self.fail("must not fetch"),
                rows=[],
                clear_env=False,
            )
        self.assertEqual(len(served), 1)
        self.assertEqual(served[0].windows[0].used_percent, 33.0)
        self.assertTrue(served[0].active)


if __name__ == "__main__":
    unittest.main()
