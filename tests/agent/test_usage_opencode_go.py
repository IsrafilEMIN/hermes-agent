import pytest

from agent import usage_opencode_go as opencode_go


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, payload, status_code=200):
        self.calls = calls
        self.payload = payload
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse(self.payload, self.status_code)


def _usage_payload():
    return {
        "usage": {
            "rolling": {"percent": 25, "status": "ok", "resetsAt": "2026-08-27T10:00:00Z"},
            "weekly": {"percent": 60, "status": "ok", "resetsAt": "2026-08-30T10:00:00Z"},
            "monthly": {"percent": 35, "status": "rate-limited", "resetsAt": "2026-09-01T10:00:00Z"},
        }
    }


def _patch_client(monkeypatch, calls, payload, status_code=200):
    monkeypatch.setattr(
        opencode_go.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, payload, status_code),
    )


def _ok_window(percent):
    return {"percent": percent, "status": "ok", "resetsAt": "2026-09-01T10:00:00Z"}


def test_opencode_go_explicit_key_hits_default_endpoint(monkeypatch):
    calls = []
    _patch_client(monkeypatch, calls, _usage_payload())
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)

    snapshot = opencode_go.fetch_opencode_go_account_usage(api_key="go-key-123")

    assert snapshot is not None
    assert snapshot.provider == "opencode-go"
    assert snapshot.source == "usage_api"
    assert snapshot.plan == "OpenCode Go"
    assert calls[0]["url"] == "https://opencode.ai/zen/go/v1/usage"
    assert calls[0]["headers"]["Accept"] == "application/json"
    assert calls[0]["headers"]["Authorization"] == "Bearer go-key-123"
    assert [w.used_percent for w in snapshot.windows] == [25.0, 60.0, 35.0]
    assert [w.id for w in snapshot.windows] == ["5h", "7d", "monthly"]
    assert [w.label for w in snapshot.windows] == ["5 Hour", "Weekly", "Monthly"]
    assert snapshot.windows[0].reset_at is not None


def test_opencode_go_v1_base_url_not_doubled(monkeypatch):
    calls = []
    _patch_client(monkeypatch, calls, _usage_payload())

    snapshot = opencode_go.fetch_opencode_go_account_usage(
        base_url="https://opencode.ai/zen/go/v1",
        api_key="go-key-123",
    )

    assert snapshot is not None
    assert calls[0]["url"] == "https://opencode.ai/zen/go/v1/usage"


def test_opencode_go_env_key_and_base_url_fallbacks(monkeypatch):
    calls = []
    _patch_client(monkeypatch, calls, _usage_payload())
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "env-key-456")
    monkeypatch.setenv("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/")

    snapshot = opencode_go.fetch_opencode_go_account_usage()

    assert snapshot is not None
    assert calls[0]["url"] == "https://opencode.ai/zen/go/v1/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer env-key-456"


@pytest.mark.parametrize(
    "rolling,weekly,monthly",
    [
        (_ok_window(25), _ok_window(60), None),
        (_ok_window(25), _ok_window(60), {"percent": 150, "status": "ok", "resetsAt": "2026-09-01T10:00:00Z"}),
        (_ok_window(25), _ok_window(60), {"percent": -1, "status": "ok", "resetsAt": "2026-09-01T10:00:00Z"}),
        (_ok_window(25), _ok_window(60), {"percent": float("nan"), "status": "ok", "resetsAt": "2026-09-01T10:00:00Z"}),
        (_ok_window(25), _ok_window(60), {"percent": 35, "status": "unknown", "resetsAt": "2026-09-01T10:00:00Z"}),
        (_ok_window(25), _ok_window(60), {"percent": 35, "status": "ok", "resetsAt": "not-a-date"}),
        (_ok_window(25), _ok_window(60), "not-a-window"),
        ("not-a-window", _ok_window(60), _ok_window(35)),
    ],
)
def test_opencode_go_malformed_usage_returns_none(monkeypatch, rolling, weekly, monthly):
    calls = []
    payload = _usage_payload()
    payload["usage"] = {"rolling": rolling, "weekly": weekly, "monthly": monthly}
    _patch_client(monkeypatch, calls, payload)

    snapshot = opencode_go.fetch_opencode_go_account_usage(api_key="go-key-123")

    assert snapshot is None


def test_opencode_go_missing_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)

    snapshot = opencode_go.fetch_opencode_go_account_usage()

    assert snapshot is None


def test_opencode_go_http_403_returns_none(monkeypatch):
    calls = []
    _patch_client(monkeypatch, calls, {}, status_code=403)

    snapshot = opencode_go.fetch_opencode_go_account_usage(api_key="go-key-123")

    assert snapshot is None
    assert calls[0]["url"] == "https://opencode.ai/zen/go/v1/usage"
