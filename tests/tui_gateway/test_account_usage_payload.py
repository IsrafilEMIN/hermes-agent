import threading
import time
from types import SimpleNamespace

from agent import account_usage
from tui_gateway import server


def _codex_agent():
    return SimpleNamespace(provider="openai-codex", _credential_pool_entry_id="entry-b")


def _snapshots():
    return (
        account_usage.AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            account_label="Codex 1",
            active=True,
            windows=(account_usage.AccountUsageWindow("Session", used_percent=9),),
        ),
    )


def _register_session(sid, session):
    with server._sessions_lock:
        server._sessions[sid] = session
    return session


def _unregister_session(sid):
    with server._sessions_lock:
        server._sessions.pop(sid, None)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _patch_emit_and_reconcile(monkeypatch, emitted):
    monkeypatch.setattr(
        server,
        "_emit",
        lambda ev, sid, payload=None: emitted.append((ev, sid, payload or {})),
    )
    monkeypatch.setattr(server, "_reconcile_session_cwd_from_terminal", lambda _s: False)


def test_session_usage_includes_safe_pool_accounts(monkeypatch):
    agent = SimpleNamespace(
        provider="openai-codex",
        _credential_pool_entry_id="entry-b",
    )
    snapshots = (
        account_usage.AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            account_label="Codex 1",
            active=False,
            windows=(account_usage.AccountUsageWindow("Session", used_percent=13),),
        ),
        account_usage.AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            account_label="Codex 2",
            active=True,
            windows=(account_usage.AccountUsageWindow("Session", used_percent=58),),
        ),
    )
    seen = {}

    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 1, "total": 10})

    def fake_fetch(provider, *, active_entry_id=None, fresh=False):
        seen.update(provider=provider, active_entry_id=active_entry_id, fresh=fresh)
        return snapshots

    monkeypatch.setattr(account_usage, "fetch_pool_account_usage", fake_fetch)
    result = server._session_usage_snapshot({"agent": agent})

    assert seen == {
        "provider": "openai-codex",
        "active_entry_id": "entry-b",
        "fresh": False,
    }
    assert result["calls"] == 1
    assert [item["label"] for item in result["accounts"]] == ["Codex 1", "Codex 2"]
    assert [item["active"] for item in result["accounts"]] == [False, True]
    assert "credential_id" not in repr(result["accounts"])


def test_session_usage_pool_failure_is_fail_open(monkeypatch):
    agent = SimpleNamespace(provider="openai-codex", _credential_pool_entry_id="entry-a")
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 2, "total": 20})
    monkeypatch.setattr(
        account_usage,
        "fetch_pool_account_usage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic")),
    )

    assert server._session_usage_snapshot({"agent": agent}) == {"calls": 2, "total": 20}


def test_session_usage_fresh_flag_reaches_pool_fetch(monkeypatch):
    agent = SimpleNamespace(provider="openai-codex", _credential_pool_entry_id="entry-b")
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 1, "total": 10})
    seen = {}

    def fake_fetch(provider, *, active_entry_id=None, fresh=False):
        seen.update(provider=provider, active_entry_id=active_entry_id, fresh=fresh)
        return ()

    monkeypatch.setattr(account_usage, "fetch_pool_account_usage", fake_fetch)

    assert server._session_usage_snapshot({"agent": agent}, fresh=True) == {
        "calls": 1,
        "total": 10,
    }
    assert seen == {"provider": "openai-codex", "active_entry_id": "entry-b", "fresh": True}


def test_session_usage_default_keeps_the_pool_cache(monkeypatch):
    """Non-end-of-turn emissions must keep the cached pool numbers."""
    agent = SimpleNamespace(provider="openai-codex", _credential_pool_entry_id="entry-a")
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 2, "total": 20})
    seen = {}

    def fake_fetch(provider, *, active_entry_id=None, fresh=False):
        seen.update(fresh=fresh)
        return ()

    monkeypatch.setattr(account_usage, "fetch_pool_account_usage", fake_fetch)
    server._session_usage_snapshot({"agent": agent})
    assert seen == {"fresh": False}


def test_settled_session_info_emits_cached_then_schedules_background_refresh(monkeypatch):
    """The end-of-turn emission is immediate (cached usage) and schedules the
    fresh pooled-Codex fetch on the bounded background worker: a blocked fetch
    cannot delay the emit (or a queued prompt's next turn), and the worker
    refreshes with fresh=True exactly once."""
    agent = _codex_agent()
    session = _register_session(
        "sid-bg", {"agent": agent, "session_key": "sess-bg", "cwd": ""}
    )
    emitted = []
    fetch_calls = []
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    _patch_emit_and_reconcile(monkeypatch, emitted)
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 1, "total": 10})

    def fake_fetch(provider, *, active_entry_id=None, fresh=False):
        fetch_calls.append((provider, active_entry_id, fresh))
        if not fresh:
            return ()  # cached path: no accounts yet
        fetch_started.set()
        assert release_fetch.wait(timeout=5.0)
        return _snapshots()

    monkeypatch.setattr(account_usage, "fetch_pool_account_usage", fake_fetch)

    try:
        server._emit_settled_session_info("sid-bg", session, agent)

        # The cached emit lands while the fresh fetch is still blocked: the
        # turn loop was never held up by the HTTP usage fetch.
        assert fetch_started.wait(timeout=5.0)
        assert [ev for ev, _sid, _p in emitted] == ["session.info"]
        event, sid, payload = emitted[0]
        assert (event, sid) == ("session.info", "sid-bg")
        assert payload["usage"]["calls"] == 1
        assert "accounts" not in payload["usage"]

        # The worker refreshes exactly once, with fresh=True, for the pooled
        # Codex account; the immediate cached emit used fresh=False.
        release_fetch.set()
        assert _wait_until(lambda: len(emitted) == 2)
        event, sid, payload = emitted[1]
        assert (event, sid) == ("session.info", "sid-bg")
        assert payload["usage"]["accounts"][0]["label"] == "Codex 1"
        assert payload["usage"]["accounts"][0]["windows"][0]["used_percent"] == 9
        assert "credential_id" not in repr(payload["usage"]["accounts"])
        assert fetch_calls == [
            ("openai-codex", "entry-b", False),
            ("openai-codex", "entry-b", True),
        ]
    finally:
        release_fetch.set()
        _unregister_session("sid-bg")


def test_settled_usage_refresh_stale_generation_is_suppressed(monkeypatch):
    """A slow refresh from an older settled turn never overwrites the newer
    turn's emission, even when it completes after it."""
    agent = _codex_agent()
    session = _register_session(
        "sid-stale", {"agent": agent, "session_key": "sess-stale", "cwd": ""}
    )
    emitted = []
    fetch_calls = []
    first_fresh_started = threading.Event()
    release_first_fresh = threading.Event()
    _patch_emit_and_reconcile(monkeypatch, emitted)
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 1, "total": 10})

    def fake_fetch(provider, *, active_entry_id=None, fresh=False):
        fetch_calls.append((provider, active_entry_id, fresh))
        if not fresh:
            return ()
        if len(fetch_calls) == 2:
            # Turn 1's worker: block until turn 2 has settled AND completed.
            first_fresh_started.set()
            assert release_first_fresh.wait(timeout=5.0)
        return _snapshots()

    monkeypatch.setattr(account_usage, "fetch_pool_account_usage", fake_fetch)

    try:
        server._emit_settled_session_info("sid-stale", session, agent)  # turn 1
        assert first_fresh_started.wait(timeout=5.0)
        server._emit_settled_session_info("sid-stale", session, agent)  # turn 2
        # cached(turn1) + cached(turn2) + updated(turn2)
        assert _wait_until(lambda: len(emitted) == 3)

        # Turn 1's worker finishes last — its emit must be suppressed.
        release_first_fresh.set()
        time.sleep(0.3)
        assert len(emitted) == 3
        assert fetch_calls.count(("openai-codex", "entry-b", True)) == 2
        # Exactly one updated payload exists, and it came from the newer turn.
        updated = [p for ev, _sid, p in emitted if p.get("usage", {}).get("accounts")]
        assert len(updated) == 1
        assert updated[0]["usage"]["accounts"][0]["label"] == "Codex 1"
    finally:
        release_first_fresh.set()
        _unregister_session("sid-stale")


def test_settled_usage_refresh_skips_emit_when_session_no_longer_live(monkeypatch):
    """A refresh whose session was closed before completion does not emit to
    the dead sid (session/agent liveness is verified before the final emit)."""
    agent = _codex_agent()
    session = _register_session(
        "sid-gone", {"agent": agent, "session_key": "sess-gone", "cwd": ""}
    )
    emitted = []
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    _patch_emit_and_reconcile(monkeypatch, emitted)
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 1, "total": 10})

    def fake_fetch(provider, *, active_entry_id=None, fresh=False):
        if not fresh:
            return ()
        fetch_started.set()
        assert release_fetch.wait(timeout=5.0)
        return _snapshots()

    monkeypatch.setattr(account_usage, "fetch_pool_account_usage", fake_fetch)

    try:
        server._emit_settled_session_info("sid-gone", session, agent)
        assert fetch_started.wait(timeout=5.0)
        assert len(emitted) == 1
        # The session is closed while the refresh is in flight.
        _unregister_session("sid-gone")
        release_fetch.set()
        time.sleep(0.3)
        assert len(emitted) == 1  # suppressed: session no longer live
    finally:
        release_fetch.set()
        _unregister_session("sid-gone")


def test_settled_usage_refresh_failure_is_fail_open(monkeypatch):
    """A failing worker fetch leaves the cached emission as the source of
    truth and never crashes the turn loop or leaks secrets."""
    agent = _codex_agent()
    session = _register_session(
        "sid-fail", {"agent": agent, "session_key": "sess-fail", "cwd": ""}
    )
    emitted = []
    _patch_emit_and_reconcile(monkeypatch, emitted)

    def fake_fetch(provider, *, active_entry_id=None, fresh=False):
        return ()

    monkeypatch.setattr(account_usage, "fetch_pool_account_usage", fake_fetch)

    def flaky_snapshot(session, *, fresh=False):
        if fresh:
            raise RuntimeError("synthetic worker failure")
        return {"calls": 2, "total": 20}

    monkeypatch.setattr(server, "_session_usage_snapshot", flaky_snapshot)

    try:
        server._emit_settled_session_info("sid-fail", session, agent)
        assert _wait_until(lambda: len(emitted) == 1)
        assert emitted[0][0] == "session.info"
        assert emitted[0][2]["usage"] == {"calls": 2, "total": 20}
    finally:
        _unregister_session("sid-fail")


def test_settled_usage_refresh_scheduling_failure_is_fail_open(monkeypatch):
    """If the bounded executor rejects the refresh task, the cached emission
    still lands and the bookkeeping entry is cleaned up."""
    agent = _codex_agent()
    session = _register_session(
        "sid-nopool", {"agent": agent, "session_key": "sess-nopool", "cwd": ""}
    )
    emitted = []
    _patch_emit_and_reconcile(monkeypatch, emitted)
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {"calls": 1, "total": 10})
    monkeypatch.setattr(
        account_usage,
        "fetch_pool_account_usage",
        lambda provider, *, active_entry_id=None, fresh=False: (),
    )

    class RejectingExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("executor shutting down")

    monkeypatch.setattr(server, "_pool", RejectingExecutor())

    try:
        server._emit_settled_session_info("sid-nopool", session, agent)
        assert len(emitted) == 1
        assert (emitted[0][0], emitted[0][1]) == ("session.info", "sid-nopool")
        assert "accounts" not in emitted[0][2]["usage"]
        assert server._settled_usage_refresh_generations.get("sid-nopool") is None
    finally:
        _unregister_session("sid-nopool")
