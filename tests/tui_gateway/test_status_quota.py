from tui_gateway import server


class _Agent:
    model = "gpt-5"
    provider = "openai-codex"
    api_key = "tok"
    base_url = None


def test_get_usage_omits_quota():
    usage = server._get_usage(_Agent())
    assert "quota" not in usage


def test_session_usage_snapshot_attaches_cached_quota(monkeypatch):
    accounts = [
        {"provider": "openai-codex", "active": True, "five_hour": 21.0, "seven_day": 4.0},
        {"provider": "openai-codex", "active": False, "five_hour": 66.0},
    ]
    monkeypatch.setattr(
        "agent.account_usage.get_cached_status_quota",
        lambda provider, **kwargs: accounts,
    )
    monkeypatch.setattr(
        "agent.account_usage.schedule_status_quota_refresh",
        lambda *args, **kwargs: False,
    )
    usage = server._session_usage_snapshot({"agent": _Agent(), "session_key": "s1"})
    assert usage["quota"] == accounts
    assert "quota" not in server._get_usage(_Agent())


def test_session_usage_snapshot_sets_empty_quota(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.get_cached_status_quota",
        lambda provider, **kwargs: [],
    )
    monkeypatch.setattr(
        "agent.account_usage.schedule_status_quota_refresh",
        lambda *args, **kwargs: False,
    )
    usage = server._session_usage_snapshot({"agent": _Agent(), "session_key": "s1"})
    assert usage["quota"] == []


def test_session_usage_snapshot_sets_empty_quota_without_provider():
    class _NoProvider:
        model = "gpt-5"
        provider = ""
        api_key = "tok"
        base_url = None

    usage = server._session_usage_snapshot({"agent": _NoProvider(), "session_key": "s1"})
    assert usage["quota"] == []
