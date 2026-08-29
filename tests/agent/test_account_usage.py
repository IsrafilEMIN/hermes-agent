from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent import account_usage


def _jwt_with_claims(claims: dict) -> str:
    import base64
    import json

    def _part(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_part({'alg': 'none', 'typ': 'JWT'})}.{_part(claims)}.sig"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

class _FakeClient:
    def __init__(self, calls, payload):
        self.calls = calls
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse(self.payload)

@pytest.fixture
def codex_usage_payload():
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 21,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 4,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }

def test_codex_usage_prefers_explicit_live_agent_credentials(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("legacy auth should not be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert snapshot.provider == "openai-codex"
    assert snapshot.plan == "Plus"
    assert [w.label for w in snapshot.windows] == ["Session", "Weekly"]
    assert snapshot.windows[0].used_percent == 21
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer live-agent-token"

def test_codex_usage_falls_back_to_native_credential_pool(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    # Pool fallback fires only on AuthError (the documented "no creds" mode of
    # the resolver), NOT on arbitrary exceptions — see the transient-error guard
    # test below.
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(
            account_usage.AuthError("no singleton auth", provider="openai-codex", code="codex_auth_missing")
        ),
    )

    pool_entry = SimpleNamespace(
        runtime_api_key="pooled-token",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(select=lambda: pool_entry)

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[1].label == "Weekly"
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer pooled-token"
    # Pool creds have no account_id concept — the ChatGPT-Account-Id header must
    # be omitted rather than sent stale/wrong.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]

def test_codex_usage_account_id_read_failure_keeps_singleton_token(monkeypatch, codex_usage_payload):
    """When the resolver succeeds but the separate account_id read raises, the
    working singleton token must still be used (best-effort account_id), NOT
    abandoned in favor of a header-less pool credential."""
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: {
            "api_key": "singleton-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    monkeypatch.setattr(
        account_usage,
        "_read_codex_tokens",
        lambda *a, **k: (_ for _ in ()).throw(
            account_usage.AuthError("partial store", provider="openai-codex", code="codex_auth_invalid_shape")
        ),
    )

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(
        credential_pool,
        "load_pool",
        lambda provider: (_ for _ in ()).throw(AssertionError("pool must not be consulted")),
    )

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert calls[0]["headers"]["Authorization"] == "Bearer singleton-token"
    # account_id read failed → header omitted, but the singleton token is kept.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]

# ── Banked rate-limit reset credits (`/usage reset`) ─────────────────────────

class _FakeResetClient:
    """GET returns the usage payload; POST returns the consume payload."""

    def __init__(self, calls, usage_payload, consume_payload=None):
        self.calls = calls
        self.usage_payload = usage_payload
        self.consume_payload = consume_payload or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return _FakeResponse(self.usage_payload)

    def post(self, url, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return _FakeResponse(self.consume_payload)

def _usage_payload_with_resets(primary_used, secondary_used, banked):
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": primary_used, "reset_at": 1779846359},
            "secondary_window": {"used_percent": secondary_used, "reset_at": 1780230796},
        },
        "rate_limit_reset_credits": {"available_count": banked},
        "credits": {"has_credits": False},
    }

def test_redeem_missing_credentials_reports_unavailable(monkeypatch):
    monkeypatch.setattr(
        account_usage,
        "_resolve_codex_usage_credentials",
        lambda base_url, api_key: (_ for _ in ()).throw(RuntimeError("no creds")),
    )

    result = account_usage.redeem_codex_reset_credit()

    assert result.status == "unavailable"
    assert "hermes auth" in result.message

def _snap(provider, windows, **kwargs):
    return account_usage.AccountUsageSnapshot(
        provider=provider,
        source="test",
        fetched_at=datetime.now(timezone.utc),
        windows=tuple(windows),
        **kwargs,
    )

def test_snapshot_to_quota_account_maps_codex_session_and_weekly():
    snap = _snap(
        "openai-codex",
        [
            account_usage.AccountUsageWindow(label="Session", used_percent=21, id="5h"),
            account_usage.AccountUsageWindow(label="Weekly", used_percent=4, id="7d"),
        ],
    )
    account = account_usage.snapshot_to_quota_account(snap, active=True)
    assert account == {
        "provider": "openai-codex",
        "active": True,
        "five_hour": 21.0,
        "seven_day": 4.0,
    }

def test_snapshot_to_quota_account_skips_anthropic_opus_sonnet_windows():
    snap = _snap(
        "anthropic",
        [
            account_usage.AccountUsageWindow(label="Current session", used_percent=24, id="5h"),
            account_usage.AccountUsageWindow(label="Current week", used_percent=8, id="7d"),
            account_usage.AccountUsageWindow(label="Opus week", used_percent=90),
            account_usage.AccountUsageWindow(label="Sonnet week", used_percent=40),
        ],
    )
    account = account_usage.snapshot_to_quota_account(snap, active=True)
    assert account == {
        "provider": "anthropic",
        "active": True,
        "five_hour": 24.0,
        "seven_day": 8.0,
    }
    assert "monthly" not in account

def test_snapshot_to_quota_account_omits_missing_windows():
    snap = _snap(
        "openai-codex",
        [account_usage.AccountUsageWindow(label="Session", used_percent=24, id="5h")],
    )
    account = account_usage.snapshot_to_quota_account(snap, active=True)
    assert account["five_hour"] == 24.0
    assert "seven_day" not in account

def test_collect_status_quota_marks_one_pool_entry_active(monkeypatch):
    account_usage.reset_status_quota_cache()
    snaps = {
        "tok-a": _snap(
            "openai-codex",
            [
                account_usage.AccountUsageWindow(label="Session", used_percent=24, id="5h"),
                account_usage.AccountUsageWindow(label="Weekly", used_percent=8, id="7d"),
            ],
        ),
        "tok-b": _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=66, id="5h")],
        ),
    }

    def fake_fetch(provider, *, base_url=None, api_key=None):
        return snaps[api_key]

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key="tok-b", runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: "a" if key == "tok-a" else "b",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex", api_key="tok-a")
    assert [account["active"] for account in accounts] == [True, False]
    assert accounts[0]["five_hour"] == 24.0
    assert accounts[0]["seven_day"] == 8.0
    assert accounts[1]["five_hour"] == 66.0
    assert "seven_day" not in accounts[1]

def test_collect_status_quota_keeps_two_distinct_runtime_keys_ordered(monkeypatch):
    account_usage.reset_status_quota_cache()
    snaps = {
        "tok-a": _snap(
            "openai-codex",
            [
                account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h"),
                account_usage.AccountUsageWindow(label="Weekly", used_percent=57, id="7d"),
            ],
        ),
        "tok-b": _snap(
            "openai-codex",
            [
                account_usage.AccountUsageWindow(label="Session", used_percent=0, id="5h"),
                account_usage.AccountUsageWindow(label="Weekly", used_percent=51, id="7d"),
            ],
        ),
    }

    def fake_fetch(provider, *, base_url=None, api_key=None):
        return snaps[api_key]

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key="tok-b", runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: "a" if key == "tok-a" else "b",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex", api_key="tok-a")
    assert [account["active"] for account in accounts] == [True, False]
    assert [account["five_hour"] for account in accounts] == [25.0, 0.0]
    assert [account["seven_day"] for account in accounts] == [57.0, 51.0]

def test_collect_status_quota_fetches_duplicate_runtime_keys_once(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "openai-codex",
            [
                account_usage.AccountUsageWindow(label="Session", used_percent=75, id="5h"),
                account_usage.AccountUsageWindow(label="Weekly", used_percent=43, id="7d"),
            ],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key="tok-b", runtime_base_url=None)
    entry_a2 = SimpleNamespace(id="a2", runtime_api_key="tok-a", runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b, entry_a2],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: "a" if key == "tok-a" else "b",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex", api_key="tok-a")
    assert keys == ["tok-a", "tok-b"]
    assert len(accounts) == 2
    assert [account["active"] for account in accounts] == [True, False]

def test_collect_status_quota_skips_empty_runtime_key(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=75, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_empty = SimpleNamespace(id="e", runtime_api_key="", runtime_base_url=None)
    entry_a = SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_empty, entry_a],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: "a",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex", api_key="tok-a")
    assert keys == ["tok-a"]
    assert len(accounts) == 1
    assert accounts[0]["active"] is True

def test_collect_status_quota_all_empty_keys_fail_closed_without_fallback(monkeypatch):
    account_usage.reset_status_quota_cache()
    calls = {"n": 0}

    def fake_fetch(provider, *, base_url=None, api_key=None):
        calls["n"] += 1
        raise AssertionError("provider fallback must not run")

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_empty = SimpleNamespace(id="e", runtime_api_key="", runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_empty],
        current=lambda: entry_empty,
        entry_id_for_api_key=lambda key: None,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex", api_key="tok-a")
    assert accounts == []
    assert calls["n"] == 0

def test_collect_status_quota_failed_second_fetch_does_not_duplicate_first(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    snap_a = _snap(
        "openai-codex",
        [
            account_usage.AccountUsageWindow(label="Session", used_percent=75, id="5h"),
            account_usage.AccountUsageWindow(label="Weekly", used_percent=43, id="7d"),
        ],
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        if api_key == "tok-b":
            raise RuntimeError("boom")
        return snap_a

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key="tok-b", runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: "a" if key == "tok-a" else "b",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex", api_key="tok-a")
    assert keys == ["tok-a", "tok-b"]
    assert len(accounts) == 1
    assert accounts[0]["five_hour"] == 75.0
    assert accounts[0]["seven_day"] == 43.0
    assert accounts[0]["active"] is True

def test_collect_status_quota_caps_distinct_pool_at_four(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=10, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entries = [
        SimpleNamespace(id=f"e{i}", runtime_api_key=f"tok-{i}", runtime_base_url=None)
        for i in range(6)
    ]
    pool = SimpleNamespace(
        entries=lambda: entries,
        current=lambda: entries[0],
        entry_id_for_api_key=lambda key: "e0",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == ["tok-0", "tok-1", "tok-2", "tok-3"]
    assert len(accounts) == 4
    assert [account["active"] for account in accounts] == [True, False, False, False]

def test_collect_status_quota_codex_same_identity_different_tokens_fetch_once(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_a = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "Alice@Example.com"},
        }
    )
    tok_b = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key=tok_a, runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key=tok_b, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: entry_a.id,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_a]
    assert len(accounts) == 1
    assert accounts[0]["active"] is True

def test_collect_status_quota_codex_same_email_different_workspaces_remain_two(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_a = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )
    tok_b = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-2"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key=tok_a, runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key=tok_b, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: entry_a.id,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_a, tok_b]
    assert len(accounts) == 2
    assert [account["active"] for account in accounts] == [True, False]

def test_collect_status_quota_codex_same_workspace_different_emails_remain_two(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_a = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )
    tok_b = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "bob@example.com"},
        }
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key=tok_a, runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key=tok_b, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: entry_a.id,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_a, tok_b]
    assert len(accounts) == 2
    assert [account["active"] for account in accounts] == [True, False]

def test_collect_status_quota_codex_opaque_distinct_tokens_remain_two(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_a = "opaque-token-a"
    tok_b = "opaque-token-b"

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key=tok_a, runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key=tok_b, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: entry_a.id,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_a, tok_b]
    assert len(accounts) == 2
    assert [account["active"] for account in accounts] == [True, False]

def test_collect_status_quota_codex_active_rotated_entry_beats_stale(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_stale = _jwt_with_claims(
        {
            "exp": 1,
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )
    tok_new = _jwt_with_claims(
        {
            "exp": 2,
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        if api_key == tok_stale:
            raise AssertionError("stale candidate must not be fetched before active")
        return _snap(
            "openai-codex",
            [
                account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h"),
                account_usage.AccountUsageWindow(label="Weekly", used_percent=57, id="7d"),
            ],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_stale = SimpleNamespace(id="a", runtime_api_key=tok_stale, runtime_base_url=None)
    entry_active = SimpleNamespace(id="c", runtime_api_key=tok_new, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_stale, entry_active],
        current=lambda: entry_active,
        entry_id_for_api_key=lambda key: entry_active.id,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_new]
    assert len(accounts) == 1
    assert accounts[0]["active"] is True
    assert accounts[0]["five_hour"] == 25.0
    assert accounts[0]["seven_day"] == 57.0

def test_collect_status_quota_codex_fetches_active_candidate_first(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_stale = _jwt_with_claims(
        {
            "exp": 1,
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )
    tok_new = _jwt_with_claims(
        {
            "exp": 2,
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        if api_key == tok_new:
            raise RuntimeError("boom")
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_stale = SimpleNamespace(id="a", runtime_api_key=tok_stale, runtime_base_url=None)
    entry_active = SimpleNamespace(id="c", runtime_api_key=tok_new, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_stale, entry_active],
        current=lambda: entry_active,
        entry_id_for_api_key=lambda key: entry_active.id,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_new, tok_stale]
    assert len(accounts) == 1
    assert accounts[0]["active"] is True
    assert accounts[0]["five_hour"] == 25.0

def test_collect_status_quota_codex_identity_falls_back_when_first_candidate_fails(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_a = _jwt_with_claims(
        {
            "exp": 1,
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )
    tok_b = _jwt_with_claims(
        {
            "exp": 2,
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-1"},
            "https://api.openai.com/profile": {"email": "alice@example.com"},
        }
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        if api_key == tok_a:
            raise RuntimeError("boom")
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=25, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key=tok_a, runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key=tok_b, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: None,
        entry_id_for_api_key=lambda key: None,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_a, tok_b]
    assert len(accounts) == 1
    assert accounts[0]["active"] is True

def test_collect_status_quota_codex_dropped_duplicate_keeps_active_off_unrelated_account(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []
    tok_a = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-x"},
            "https://api.openai.com/profile": {"email": "x@example.com"},
        }
    )
    tok_c = _jwt_with_claims(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "ws-y"},
            "https://api.openai.com/profile": {"email": "y@example.com"},
        }
    )

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        snap_key = {
            tok_a: 25.0,
            tok_c: 10.0,
        }[api_key]
        return _snap(
            "openai-codex",
            [account_usage.AccountUsageWindow(label="Session", used_percent=snap_key, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key=tok_a, runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key=tok_a, runtime_base_url=None)
    entry_c = SimpleNamespace(id="c", runtime_api_key=tok_c, runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b, entry_c],
        current=lambda: entry_c,
        entry_id_for_api_key=lambda key: entry_c.id,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("openai-codex")
    assert keys == [tok_a, tok_c]
    assert len(accounts) == 2
    assert [account["active"] for account in accounts] == [False, True]
    assert [account["five_hour"] for account in accounts] == [25.0, 10.0]

def test_status_quota_cache_returns_prior_result_without_refetch(monkeypatch):
    account_usage.reset_status_quota_cache()
    calls = {"n": 0}

    def fake_collect(provider, *, api_key=None, base_url=None):
        calls["n"] += 1
        return [{"provider": "openai-codex", "active": True, "five_hour": 21.0, "seven_day": 4.0}]

    monkeypatch.setattr(account_usage, "collect_status_quota", fake_collect)
    first = account_usage.refresh_status_quota("openai-codex")
    cached = account_usage.get_cached_status_quota("openai-codex")
    scheduled = account_usage.schedule_status_quota_refresh("openai-codex")
    assert first == cached
    assert cached[0]["five_hour"] == 21.0
    assert calls["n"] == 1
    assert scheduled is False


def test_snapshot_to_quota_account_maps_cursor_monthly_rails():
    snap = _snap(
        "cursor",
        [
            account_usage.AccountUsageWindow(label="Auto", used_percent=6, id="monthly"),
            account_usage.AccountUsageWindow(label="API", used_percent=5, id="monthly_other"),
        ],
    )
    account = account_usage.snapshot_to_quota_account(snap, active=True)
    assert account == {
        "provider": "cursor",
        "active": True,
        "monthly": 6.0,
        "monthly_other": 5.0,
    }


def test_collect_status_quota_does_not_fanout_anthropic_pool(monkeypatch):
    account_usage.reset_status_quota_cache()
    calls = {"n": 0}

    def fake_fetch(provider, *, base_url=None, api_key=None):
        calls["n"] += 1
        return _snap(
            "anthropic",
            [account_usage.AccountUsageWindow(label="Current session", used_percent=24, id="5h")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    pool = SimpleNamespace(
        entries=lambda: [
            SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None),
            SimpleNamespace(id="b", runtime_api_key="tok-b", runtime_base_url=None),
        ],
        current=lambda: None,
        entry_id_for_api_key=lambda key: None,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("anthropic")
    assert len(accounts) == 1
    assert accounts[0]["active"] is True
    assert accounts[0]["five_hour"] == 24.0
    assert calls["n"] == 1

def test_fetch_account_usage_dispatches_opencode_go(monkeypatch):
    snap = _snap(
        "opencode-go",
        [
            account_usage.AccountUsageWindow(label="5 Hour", used_percent=25, id="5h"),
            account_usage.AccountUsageWindow(label="Weekly", used_percent=60, id="7d"),
            account_usage.AccountUsageWindow(label="Monthly", used_percent=35, id="monthly"),
        ],
    )
    called = {}

    def fake_fetch(*, base_url=None, api_key=None):
        called["base_url"] = base_url
        called["api_key"] = api_key
        return snap

    monkeypatch.setattr("agent.usage_opencode_go.fetch_opencode_go_account_usage", fake_fetch)
    result = account_usage.fetch_account_usage(
        "opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="go-key",
    )
    assert result is snap
    assert called == {"base_url": "https://opencode.ai/zen/go/v1", "api_key": "go-key"}


def test_fetch_account_usage_dispatches_xai_oauth(monkeypatch):
    snap = _snap(
        "xai-oauth",
        [account_usage.AccountUsageWindow(label="Weekly", used_percent=10, id="7d")],
    )

    def fake_fetch(*, base_url=None, api_key=None):
        return snap

    monkeypatch.setattr("agent.usage_xai_oauth.fetch_xai_oauth_account_usage", fake_fetch)
    assert account_usage.fetch_account_usage("xai-oauth", api_key="oauth-token") is snap


def test_fetch_account_usage_skips_paid_xai_api_keys():
    assert account_usage.fetch_account_usage("xai", api_key="xai-api-key") is None


def test_fetch_account_usage_dispatches_cursor(monkeypatch):
    snap = _snap(
        "cursor",
        [
            account_usage.AccountUsageWindow(label="Cursor Models", used_percent=6, id="monthly"),
            account_usage.AccountUsageWindow(label="Other Models", used_percent=5, id="monthly_other"),
        ],
    )

    def fake_fetch(*, base_url=None, api_key=None):
        return snap

    monkeypatch.setattr("agent.usage_cursor.fetch_cursor_account_usage", fake_fetch)
    assert account_usage.fetch_account_usage("cursor", api_key="cursor-token") is snap


def test_collect_status_quota_fans_out_opencode_go_pool(monkeypatch):
    account_usage.reset_status_quota_cache()
    keys = []

    def fake_fetch(provider, *, base_url=None, api_key=None):
        keys.append(api_key)
        return _snap(
            "opencode-go",
            [
                account_usage.AccountUsageWindow(label="5 Hour", used_percent=25, id="5h"),
                account_usage.AccountUsageWindow(label="Weekly", used_percent=60, id="7d"),
                account_usage.AccountUsageWindow(label="Monthly", used_percent=35, id="monthly"),
            ],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    entry_a = SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None)
    entry_b = SimpleNamespace(id="b", runtime_api_key="tok-b", runtime_base_url=None)
    pool = SimpleNamespace(
        entries=lambda: [entry_a, entry_b],
        current=lambda: entry_a,
        entry_id_for_api_key=lambda key: "a" if key == "tok-a" else "b",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("opencode-go", api_key="tok-a")
    assert keys == ["tok-a", "tok-b"]
    assert [account["active"] for account in accounts] == [True, False]
    assert accounts[0]["five_hour"] == 25.0
    assert accounts[0]["monthly"] == 35.0


def test_collect_status_quota_does_not_fanout_cursor_pool(monkeypatch):
    account_usage.reset_status_quota_cache()
    calls = {"n": 0}

    def fake_fetch(provider, *, base_url=None, api_key=None):
        calls["n"] += 1
        return _snap(
            "cursor",
            [account_usage.AccountUsageWindow(label="Cursor Models", used_percent=6, id="monthly")],
        )

    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)
    pool = SimpleNamespace(
        entries=lambda: [
            SimpleNamespace(id="a", runtime_api_key="tok-a", runtime_base_url=None),
            SimpleNamespace(id="b", runtime_api_key="tok-b", runtime_base_url=None),
        ],
        current=lambda: None,
        entry_id_for_api_key=lambda key: None,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    accounts = account_usage.collect_status_quota("cursor", api_key="tok-a")
    assert len(accounts) == 1
    assert accounts[0]["monthly"] == 6.0
    assert calls["n"] == 1


def test_snapshot_to_quota_account_drops_xai_oauth_monthly():
    weekly_and_monthly = _snap(
        "xai-oauth",
        [
            account_usage.AccountUsageWindow(label="Weekly", used_percent=20, id="7d"),
            account_usage.AccountUsageWindow(label="Monthly", used_percent=35, id="monthly"),
        ],
    )
    account = account_usage.snapshot_to_quota_account(weekly_and_monthly, active=True)
    assert account == {"provider": "xai-oauth", "active": True, "seven_day": 20.0}

    monthly_only = _snap(
        "xai-oauth",
        [account_usage.AccountUsageWindow(label="Monthly", used_percent=35, id="monthly")],
    )
    assert account_usage.snapshot_to_quota_account(monthly_only, active=True) is None


def test_snapshot_to_quota_account_keeps_opencode_go_monthly():
    snap = _snap(
        "opencode-go",
        [
            account_usage.AccountUsageWindow(label="5 Hour", used_percent=25, id="5h"),
            account_usage.AccountUsageWindow(label="Weekly", used_percent=60, id="7d"),
            account_usage.AccountUsageWindow(label="Monthly", used_percent=35, id="monthly"),
        ],
    )
    account = account_usage.snapshot_to_quota_account(snap, active=True)
    assert account == {
        "provider": "opencode-go",
        "active": True,
        "five_hour": 25.0,
        "seven_day": 60.0,
        "monthly": 35.0,
    }
