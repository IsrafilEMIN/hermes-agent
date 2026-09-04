import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import fork_update


def test_resolve_source_root_explicit_hint_wins_over_ambient_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    ambient = tmp_path / "ambient"
    for source in (explicit, ambient):
        (source / "hermes_cli").mkdir(parents=True)
        (source / ".git").write_text("gitdir: .git\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_SOURCE_ROOT", str(ambient))

    assert fork_update.resolve_source_root(explicit) == explicit.resolve()


def test_mint_session_token_is_urlsafe():
    token = fork_update._mint_session_token()
    assert len(token) >= 32
    assert all(c.isalnum() or c in "-_" for c in token)


def test_resolve_backend_endpoint_is_fixed(tmp_path):
    assert fork_update._resolve_backend_endpoint(tmp_path, tmp_path) == ("127.0.0.1", 8642)


def test_write_desktop_record_creates_record(tmp_path, monkeypatch):
    monkeypatch.setattr(fork_update, "_git_out", lambda *a, **k: "abc1234")
    monkeypatch.setattr(fork_update, "_backend_version", lambda: "9.9.9")
    fork_update._write_desktop_record(tmp_path, tmp_path, "127.0.0.1", 8642, 4242, "tok-1")
    path = tmp_path / fork_update.DESKTOP_READY_NAME
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schemaVersion"] == 1
    assert record["kind"] == "hermes-serve-safehouse"
    assert record["owner"] == "hermes-serve-safehouse"
    assert record["host"] == "127.0.0.1"
    assert record["port"] == 8642
    assert record["pid"] == 4242
    assert record["generation"] == 1
    assert record["token"] == "tok-1"
    assert record["version"] == "9.9.9"
    assert record["gitSha"] == "abc1234"
    assert record["launchCommand"] == "hermes-backend-start"
    assert record["restartCommand"] == "hermes-backend-restart"
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_desktop_record_bumps_generation_and_keeps_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(fork_update, "_git_out", lambda *a, **k: "abc1234")
    monkeypatch.setattr(fork_update, "_backend_version", lambda: "9.9.9")
    (tmp_path / fork_update.DESKTOP_READY_NAME).write_text(
        json.dumps({"generation": 7, "schemaVersion": 1}), encoding="utf-8"
    )
    fork_update._write_desktop_record(tmp_path, tmp_path, "127.0.0.1", 8642, 4242, "tok-2")
    record = json.loads((tmp_path / fork_update.DESKTOP_READY_NAME).read_text(encoding="utf-8"))
    assert record["generation"] == 8
    assert record["schemaVersion"] == 1


def test_install_backend_launchd_writes_plist(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_SOURCE_ROOT", raising=False)
    source = tmp_path / "source"
    (source / "hermes_cli").mkdir(parents=True)
    (source / ".git").write_text("gitdir: .git\n", encoding="utf-8")
    plist_path = fork_update.install_backend_launchd(source_root=source, home=tmp_path / "home")
    assert plist_path.name == "ai.hermes.backend.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == "ai.hermes.backend"
    assert payload["RunAtLoad"] is True
    args = payload["ProgramArguments"]
    assert any(str(a).endswith("safehouse") for a in args)
    assert (
        f"--add-dirs-ro={tmp_path / 'home' / 'Library' / 'Application Support' / 'Hermes' / 'composer-images'}"
        in args
    )
    assert (
        tmp_path / "home" / "Library" / "Application Support" / "Hermes" / "composer-images"
    ).is_dir()
    assert any(str(a).endswith("hermes-backend-watch") for a in args)
    assert payload["WorkingDirectory"] == str(source)
    assert payload["KeepAlive"] == {"SuccessfulExit": False}


def test_uninstall_backend_launchd_removes_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fork_update.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0})(),
    )
    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / "ai.hermes.backend.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("{}")
    fork_update.uninstall_backend_launchd(home=tmp_path / "home")
    assert not plist_path.exists()





def test_update_main_restarts_backend_when_updated(monkeypatch):
    def fake_update(**kwargs):
        return fork_update.ForkUpdateResult(status="updated", exit_code=0, source_root="/x")

    calls = []

    monkeypatch.setattr(fork_update, "run_fork_aware_update", fake_update)
    monkeypatch.setattr(fork_update, "restart_managed_backend", lambda: calls.append("restart"))
    monkeypatch.setattr(fork_update, "_push_fork_to_origin", lambda: calls.append("push") or True)
    monkeypatch.setattr(fork_update, "_rebuild_desktop", lambda: calls.append("rebuild"))

    assert fork_update.main(["--yes"]) == 0
    assert calls == ["push", "rebuild", "restart"]


def test_update_main_skips_restart_when_up_to_date(monkeypatch):
    def fake_update(**kwargs):
        return fork_update.ForkUpdateResult(status="up_to_date", exit_code=0, source_root="/x")

    calls = []

    monkeypatch.setattr(fork_update, "run_fork_aware_update", fake_update)
    monkeypatch.setattr(fork_update, "restart_managed_backend", lambda: calls.append("restart"))
    monkeypatch.setattr(fork_update, "_push_fork_to_origin", lambda: calls.append("push") or True)
    monkeypatch.setattr(fork_update, "_rebuild_desktop", lambda: calls.append("rebuild"))

    assert fork_update.main(["--yes"]) == 0
    assert calls == ["push"]


def test_update_command_pushes_successful_up_to_date_fork(tmp_path, monkeypatch):
    from hermes_cli import update_cmd

    monkeypatch.setattr(
        fork_update,
        "run_fork_aware_update",
        lambda **kwargs: fork_update.ForkUpdateResult(status="up_to_date", exit_code=0),
    )
    monkeypatch.setattr(update_cmd, "_has_upstream_remote", lambda *args: True)
    pushed = []
    monkeypatch.setattr(update_cmd, "_sync_fork_with_upstream", lambda *args: pushed.append(args) or True)
    update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)
    assert pushed == [(["git"], tmp_path)]

def test_update_main_skips_restart_on_check(monkeypatch):
    def fake_update(**kwargs):
        return fork_update.ForkUpdateResult(status="updated", exit_code=0, source_root="/x")

    calls = []

    monkeypatch.setattr(fork_update, "run_fork_aware_update", fake_update)
    monkeypatch.setattr(fork_update, "restart_managed_backend", lambda: calls.append("restart"))
    monkeypatch.setattr(fork_update, "_push_fork_to_origin", lambda: calls.append("push") or True)
    monkeypatch.setattr(fork_update, "_rebuild_desktop", lambda: calls.append("rebuild"))

    assert fork_update.main(["--check"]) == 0
    assert calls == []


def test_update_main_no_desktop_build_flag(monkeypatch):
    def fake_update(**kwargs):
        return fork_update.ForkUpdateResult(status="updated", exit_code=0, source_root="/x")

    calls = []

    monkeypatch.setattr(fork_update, "run_fork_aware_update", fake_update)
    monkeypatch.setattr(fork_update, "restart_managed_backend", lambda: calls.append("restart"))
    monkeypatch.setattr(fork_update, "_push_fork_to_origin", lambda: calls.append("push") or True)
    monkeypatch.setattr(fork_update, "_rebuild_desktop", lambda: calls.append("rebuild"))

    assert fork_update.main(["--yes", "--no-desktop-build"]) == 0
    assert calls == ["push", "restart"]


def test_foreground_start_keeps_serve_attached(tmp_path, monkeypatch):
    calls = {}

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self):
            self.polls = iter((None, 0))

        def poll(self):
            return next(self.polls, 0)

    def spawn(source, host, port, token, detach=True):
        calls["detach"] = detach
        return FakeProcess()

    monkeypatch.setattr(fork_update, "resolve_source_root", lambda source_root=None: tmp_path)
    monkeypatch.setattr(fork_update, "_runtime_home", lambda: tmp_path)
    monkeypatch.setattr(fork_update, "_backend_listener_pid", lambda host, port: 0)
    monkeypatch.setattr(fork_update, "_spawn_serve", spawn)
    monkeypatch.setattr(fork_update, "_wait_port", lambda host, port: True)
    monkeypatch.setattr(fork_update.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(fork_update, "_git_out", lambda *args, **kwargs: "abc1234")
    monkeypatch.setattr(fork_update, "_backend_version", lambda: "9.9.9")

    assert fork_update.start_managed_backend(foreground=True) == 4242
    assert calls["detach"] is False


def test_serve_like_command_matches_backend_cmdline():
    assert fork_update._serve_like_command("") is False
    assert fork_update._serve_like_command("/sbin/launchd") is False
    assert (
        fork_update._serve_like_command(
            "/Users/kutluk/Developer/harness/hermes-agent/venv/bin/python "
            "/Users/kutluk/Developer/harness/hermes-agent/hermes serve "
            "--host 127.0.0.1 --port 8642"
        )
        is True
    )
    assert (
        fork_update._serve_like_command(
            "/Users/kutluk/Developer/harness/hermes-agent/venv/bin/python "
            "-c import time; time.sleep(30) serve --host 127.0.0.1 --port 8642"
        )
        is True
    )
    assert fork_update._serve_like_command("python -m pytest") is False


def test_serve_like_pid_uses_ps_output(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "4242" in cmd:
            return type("R", (), {"returncode": 0, "stdout": "python hermes serve --port 8642"})()
        return type("R", (), {"returncode": 1, "stdout": ""})()

    monkeypatch.setattr(fork_update.subprocess, "run", fake_run)
    assert fork_update._serve_like_pid(4242) is True
    assert fork_update._serve_like_pid(1) is False
    assert fork_update._serve_like_pid(0) is False
    assert calls[0][0] == "ps"


def test_stop_recorded_backend_skips_non_serve_pids(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(fork_update, "_runtime_home", lambda: tmp_path)
    monkeypatch.setattr(
        fork_update,
        "_stop_pid",
        lambda pid: killed.append(pid),
    )
    monkeypatch.setattr(
        fork_update,
        "_serve_like_pid",
        lambda pid: pid == 4242,
    )
    (tmp_path / fork_update.DESKTOP_READY_NAME).write_text(
        json.dumps({"pid": 1}), encoding="utf-8"
    )
    (tmp_path / fork_update.READY_RECORD_NAME).write_text(
        json.dumps({"pid": 4242}), encoding="utf-8"
    )
    fork_update._stop_recorded_backend(tmp_path)
    assert killed == [4242]

def test_backend_listener_pid_falls_back_to_record(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fork_update.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(),
    )
    monkeypatch.setattr(fork_update, "_runtime_home", lambda: tmp_path)
    (tmp_path / fork_update.DESKTOP_READY_NAME).write_text(
        json.dumps({"pid": 4242}), encoding="utf-8"
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        assert fork_update._backend_listener_pid("127.0.0.1", port) == 4242
    assert fork_update._backend_listener_pid("127.0.0.1", 1) == 0


def _git_repo(cwd, *args):
    import subprocess as sp

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "fork-test",
        "GIT_AUTHOR_EMAIL": "fork-test@example.com",
        "GIT_COMMITTER_NAME": "fork-test",
        "GIT_COMMITTER_EMAIL": "fork-test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    result = sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def _write(cwd, name, content):
    (Path(cwd) / name).write_text(content, encoding="utf-8")
    _git_repo(cwd, "add", "-A")


def _build_fork_fixture(tmp_path):
    home = tmp_path / "repo"
    home.mkdir()
    upstream = home / "upstream-work"
    upstream.mkdir()
    _git_repo(upstream, "init", "-b", "main")
    _write(upstream, "README.md", "upstream-base\n")
    _git_repo(upstream, "commit", "-m", "upstream base")
    upstream_bare = home / "upstream.git"
    _git_repo(home, "clone", "--bare", str(upstream), str(upstream_bare))
    checkout = home / "checkout"
    _git_repo(home, "clone", str(upstream_bare), str(checkout))
    _git_repo(checkout, "remote", "add", "upstream", str(upstream_bare))
    _git_repo(checkout, "fetch", "upstream", "main")
    _write(checkout, "README.md", "fork readme\n")
    _write(checkout, "fork.txt", "fork commit\n")
    _git_repo(checkout, "commit", "-m", "fork-only change")
    return checkout


def _advance_upstream(tmp_path, conflicting):
    home = tmp_path / "repo"
    work = home / "upstream-advance"
    _git_repo(home, "clone", str(home / "upstream.git"), str(work))
    if conflicting:
        _write(work, "README.md", "upstream-conflict\n")
    else:
        _write(work, "upstream-new.txt", "new upstream file\n")
    _git_repo(work, "commit", "-m", "upstream advance")
    _git_repo(work, "push", "origin", "main")


def test_fork_aware_update_rebases_commits_and_preserves_wip(tmp_path, monkeypatch):
    import hermes_cli.fork_update as fu

    checkout = _build_fork_fixture(tmp_path)
    monkeypatch.setenv("HERMES_SOURCE_ROOT", str(checkout))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fu, "OFFICIAL_REPO_URL", str(tmp_path / "repo" / "upstream.git"))
    venv_bin = checkout / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv_bin / "python").chmod(0o755)
    uv_bin = tmp_path / "home" / "bin"
    uv_bin.mkdir(parents=True)
    (uv_bin / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (uv_bin / "uv").chmod(0o755)
    _write(checkout, "wip.txt", "wip change\n")
    _advance_upstream(tmp_path, conflicting=False)
    result = fu.run_fork_aware_update(assume_yes=True)
    assert result.exit_code == 0, result.report
    assert result.status == "updated"
    assert _git_repo(checkout, "rev-list", "--count", "upstream/main..HEAD") == "1"
    assert (checkout / "fork.txt").read_text(encoding="utf-8") == "fork commit\n"
    assert (checkout / "wip.txt").read_text(encoding="utf-8") == "wip change\n"
    assert (checkout / "upstream-new.txt").exists()
    assert _git_repo(checkout, "stash", "list") == ""


def test_fork_aware_update_conflict_pauses_with_wip_autostashed(tmp_path, monkeypatch):
    import hermes_cli.fork_update as fu

    checkout = _build_fork_fixture(tmp_path)
    monkeypatch.setenv("HERMES_SOURCE_ROOT", str(checkout))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fu, "OFFICIAL_REPO_URL", str(tmp_path / "repo" / "upstream.git"))
    _write(checkout, "wip.txt", "wip change\n")
    pre_head = _git_repo(checkout, "rev-parse", "HEAD")
    _advance_upstream(tmp_path, conflicting=True)
    result = fu.run_fork_aware_update(assume_yes=True)
    assert result.exit_code == fu.CONFLICT_EXIT
    assert result.status == "conflict"
    assert _git_repo(checkout, "rev-parse", "HEAD") != pre_head
    assert not (checkout / "wip.txt").exists()
    assert (checkout / ".git" / "rebase-merge" / "autostash").exists()
    assert "rebase paused with Git autostash active" in result.report
    assert "git rebase --continue" in result.report
    assert "git rebase --abort" in result.report
    _write(checkout, "README.md", "fork readme\n")
    _git_repo(checkout, "-c", "core.editor=true", "rebase", "--continue")
    assert _git_repo(checkout, "rev-list", "--count", "upstream/main..HEAD") == "1"
    assert (checkout / "wip.txt").read_text(encoding="utf-8") == "wip change\n"
    assert _git_repo(checkout, "stash", "list") == ""


def test_fork_aware_update_conflict_abort_restores_head_and_wip(tmp_path, monkeypatch):
    import hermes_cli.fork_update as fu

    checkout = _build_fork_fixture(tmp_path)
    monkeypatch.setenv("HERMES_SOURCE_ROOT", str(checkout))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fu, "OFFICIAL_REPO_URL", str(tmp_path / "repo" / "upstream.git"))
    _write(checkout, "wip.txt", "wip change\n")
    pre_head = _git_repo(checkout, "rev-parse", "HEAD")
    _advance_upstream(tmp_path, conflicting=True)
    result = fu.run_fork_aware_update(assume_yes=True)
    assert result.exit_code == fu.CONFLICT_EXIT
    _git_repo(checkout, "rebase", "--abort")
    assert _git_repo(checkout, "rev-parse", "HEAD") == pre_head
    assert (checkout / "wip.txt").read_text(encoding="utf-8") == "wip change\n"
    assert _git_repo(checkout, "stash", "list") == ""


def test_fork_aware_update_stays_conflicted_while_rebase_paused(tmp_path, monkeypatch):
    import hermes_cli.fork_update as fu

    checkout = _build_fork_fixture(tmp_path)
    monkeypatch.setenv("HERMES_SOURCE_ROOT", str(checkout))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fu, "OFFICIAL_REPO_URL", str(tmp_path / "repo" / "upstream.git"))
    pre_head = _git_repo(checkout, "rev-parse", "HEAD")
    _advance_upstream(tmp_path, conflicting=True)
    first = fu.run_fork_aware_update(assume_yes=True)
    assert first.exit_code == fu.CONFLICT_EXIT
    assert first.status == "conflict"
    paused_head = _git_repo(checkout, "rev-parse", "HEAD")
    assert paused_head != pre_head
    assert (checkout / ".git" / "rebase-merge").is_dir()
    assert _git_repo(checkout, "rev-parse", "upstream/main") == paused_head
    assert _git_repo(checkout, "rev-list", "--count", "HEAD..upstream/main") == "0"

    follow_up = fu.run_fork_aware_update(assume_yes=True)
    assert follow_up.exit_code == fu.CONFLICT_EXIT
    assert follow_up.status == "conflict"
    assert "rebase paused" in follow_up.report
    assert "git rebase --continue" in follow_up.report
    assert follow_up.conflicted_files == ["README.md"]
    assert follow_up.recovery_path and Path(follow_up.recovery_path).exists()
    recovery = json.loads(Path(follow_up.recovery_path).read_text(encoding="utf-8"))
    assert recovery["status"] == "committed-rebase-conflict"
    assert recovery["conflicted_files"] == ["README.md"]
    assert _git_repo(checkout, "rev-parse", "HEAD") == paused_head
    assert (checkout / ".git" / "rebase-merge").is_dir()

    checked = fu.run_fork_aware_update(check_only=True)
    assert checked.exit_code == fu.CONFLICT_EXIT
    assert checked.status == "conflict"
    assert (checkout / ".git" / "rebase-merge").is_dir()


def test_fork_aware_update_stays_conflicted_with_unmerged_index(tmp_path, monkeypatch):
    import hermes_cli.fork_update as fu

    checkout = _build_fork_fixture(tmp_path)
    monkeypatch.setenv("HERMES_SOURCE_ROOT", str(checkout))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fu, "OFFICIAL_REPO_URL", str(tmp_path / "repo" / "upstream.git"))
    _git_repo(checkout, "reset", "--hard", "upstream/main")
    _write(checkout, "README.md", "local delta\n")
    _advance_upstream(tmp_path, conflicting=True)
    first = fu.run_fork_aware_update(assume_yes=True)
    assert first.exit_code == fu.CONFLICT_EXIT
    assert first.status == "conflict"
    assert _git_repo(checkout, "stash", "list") != ""
    assert _git_repo(checkout, "rev-parse", "upstream/main") == _git_repo(checkout, "rev-parse", "HEAD")

    follow_up = fu.run_fork_aware_update(assume_yes=True)
    assert follow_up.exit_code == fu.CONFLICT_EXIT
    assert follow_up.status == "conflict"
    assert "conflicted changes in the index" in follow_up.report
    assert "git stash drop" in follow_up.report
    assert follow_up.conflicted_files == ["README.md"]
    assert follow_up.recovery_path and Path(follow_up.recovery_path).exists()
    recovery = json.loads(Path(follow_up.recovery_path).read_text(encoding="utf-8"))
    assert recovery["status"] == "stash-pop-conflict"
    assert recovery["stash_ref"]
    assert _git_repo(checkout, "stash", "list") != ""

    checked = fu.run_fork_aware_update(check_only=True)
    assert checked.exit_code == fu.CONFLICT_EXIT
    assert checked.status == "conflict"


def test_fork_aware_update_clean_fork_followup_stays_up_to_date(tmp_path, monkeypatch):
    import hermes_cli.fork_update as fu

    checkout = _build_fork_fixture(tmp_path)
    monkeypatch.setenv("HERMES_SOURCE_ROOT", str(checkout))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fu, "OFFICIAL_REPO_URL", str(tmp_path / "repo" / "upstream.git"))
    venv_bin = checkout / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv_bin / "python").chmod(0o755)
    uv_bin = tmp_path / "home" / "bin"
    uv_bin.mkdir(parents=True)
    (uv_bin / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (uv_bin / "uv").chmod(0o755)
    result = fu.run_fork_aware_update(assume_yes=True)
    assert result.exit_code == 0
    assert result.status == "up_to_date"
    _advance_upstream(tmp_path, conflicting=False)
    result = fu.run_fork_aware_update(assume_yes=True)
    assert result.exit_code == 0
    assert result.status == "updated"
    result = fu.run_fork_aware_update(assume_yes=True)
    assert result.exit_code == 0
    assert result.status == "up_to_date"
    checked = fu.run_fork_aware_update(check_only=True)
    assert checked.exit_code == 0
    assert checked.status == "up_to_date"
    assert not (checkout / ".git" / "rebase-merge").exists()

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    shutil.which("bash") is None or not (_REPO_ROOT / "venv" / "bin" / "python").exists(),
    reason="needs bash and a repo venv",
)
def test_fork_update_script_execs_real_module(tmp_path):
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    script = _REPO_ROOT / "scripts" / "fork-update"
    result = subprocess.run(
        ["bash", str(script), "--help"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--restart-backend" in result.stdout
    assert "invalid choice" not in result.stderr


@pytest.mark.skipif(
    shutil.which("bash") is None or not (_REPO_ROOT / "venv" / "bin" / "python").exists(),
    reason="needs bash and a repo venv",
)
def test_restart_managed_backend_script_assembles_valid_argv(tmp_path):
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    script = _REPO_ROOT / "scripts" / "restart-managed-backend"
    result = subprocess.run(
        ["bash", str(script), "--help"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--restart-backend" in result.stdout


@pytest.mark.skipif(
    shutil.which("bash") is None or not (_REPO_ROOT / "venv" / "bin" / "python").exists(),
    reason="needs bash and a repo venv",
)
def test_scripts_hermes_forwards_fork_update_through_real_launcher(tmp_path):
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HERMES_WORKDIR="")
    script = _REPO_ROOT / "scripts" / "hermes"
    result = subprocess.run(
        ["bash", str(script), "fork-update", "--help"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--restart-backend" in result.stdout
    assert "invalid choice" not in result.stderr
