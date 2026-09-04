from __future__ import annotations

import argparse
import json
import os
import plistlib
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

OFFICIAL_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
DEFAULT_HARNESS_SOURCE = Path.home() / "Developer" / "harness" / "hermes-agent"
RECOVERY_NAME = "update-recovery.json"
DIAGNOSTIC_DIRNAME = "update-diagnostics"
RUNTIME_REFRESH_EXIT = 9
CONFLICT_EXIT = 8
ERROR_EXIT = 1
READY_RECORD_NAME = "backend-ready.json"
DESKTOP_READY_NAME = "desktop-backend.json"
DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8642
DEFAULT_LAUNCHD_LABEL = "ai.hermes.backend"


def _runtime_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _is_runtime_or_global_path(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser()
    parts = {p.lower() for p in resolved.parts}
    if ".hermes" in parts or ".local" in parts:
        return True
    text = str(resolved)
    home = str(Path.home())
    return text.startswith(home + "/.hermes") or text.startswith(home + "/.local")


def _looks_like_source(path: Path) -> bool:
    if not path:
        return False
    git = path / ".git"
    return (path / "hermes_cli").is_dir() and (git.is_dir() or git.is_file())


def resolve_source_root(hint: Optional[Path] = None) -> Path:
    # Explicit caller intent wins over ambient environment.
    if hint is not None:
        return Path(hint).expanduser().resolve()
    # Explicit environment wins over compiled-in defaults (callers and tests
    # point this at minimal git fixtures that need not contain hermes_cli).
    env = os.environ.get("HERMES_SOURCE_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    package_root = Path(__file__).resolve().parents[1]
    for candidate in (DEFAULT_HARNESS_SOURCE, package_root):
        try:
            resolved = Path(candidate).expanduser().resolve()
        except OSError:
            continue
        if _looks_like_source(resolved) and not _is_runtime_or_global_path(resolved):
            return resolved
    return DEFAULT_HARNESS_SOURCE.expanduser()


@dataclass
class ForkUpdateResult:
    status: str
    exit_code: int = 0
    source_root: str = ""
    branch: str = ""
    pre_head: str = ""
    post_head: str = ""
    commits_replayed: int = 0
    recovery_path: str = ""
    diagnostic_path: str = ""
    report: str = ""
    conflicted_files: list[str] = field(default_factory=list)

    @property
    def head_moved(self) -> bool:
        return bool(self.pre_head and self.post_head and self.pre_head != self.post_head)


def _git(git_cmd: list[str], cwd: Path, args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        git_cmd + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"},
    )


def _git_out(git_cmd: list[str], cwd: Path, args: list[str]) -> str:
    result = _git(git_cmd, cwd, args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _ensure_upstream_remote(git_cmd: list[str], cwd: Path) -> tuple[bool, str]:
    hint = "NousResearch/hermes-agent"
    got = _git(git_cmd, cwd, ["remote", "get-url", "upstream"])
    if got.returncode == 0:
        current = got.stdout.strip()
        if hint.lower() in current.lower():
            return True, current
        reset = _git(git_cmd, cwd, ["remote", "set-url", "upstream", OFFICIAL_REPO_URL])
        if reset.returncode != 0:
            return False, reset.stderr.strip() or reset.stdout.strip()
        return True, OFFICIAL_REPO_URL
    added = _git(git_cmd, cwd, ["remote", "add", "upstream", OFFICIAL_REPO_URL])
    if added.returncode != 0:
        return False, added.stderr.strip() or added.stdout.strip()
    return True, OFFICIAL_REPO_URL


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}



def _record_recovery(runtime_home: Path, payload: dict) -> Path:
    path = runtime_home / RECOVERY_NAME
    _write_json(path, payload)
    return path


def _write_diagnostic(runtime_home: Path, name: str, body: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = runtime_home / "logs" / DIAGNOSTIC_DIRNAME / f"{name}-{stamp}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _paused_rebase(git_cmd: list[str], cwd: Path) -> bool:
    for state_dir in ("rebase-merge", "rebase-apply"):
        git_path = _git_out(git_cmd, cwd, ["rev-parse", "--git-path", state_dir])
        if not git_path:
            continue
        candidate = Path(git_path)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.is_dir():
            return True
    return False


def _unmerged_index(git_cmd: list[str], cwd: Path) -> bool:
    return bool(_git_out(git_cmd, cwd, ["ls-files", "--unmerged"]))


def run_fork_aware_update(
    *,
    source_root: Optional[Path] = None,
    git_cmd: Optional[list[str]] = None,
    branch: str = "main",
    assume_yes: bool = False,
    check_only: bool = False,
) -> ForkUpdateResult:
    cwd = resolve_source_root(source_root)
    runtime_home = _runtime_home()
    git_cmd = list(git_cmd or ["git"])
    result = ForkUpdateResult(status="error", exit_code=ERROR_EXIT, source_root=str(cwd), branch=branch)

    if not (cwd / ".git").exists():
        result.report = f"source checkout is not a git repository: {cwd}"
        return result
    if _is_runtime_or_global_path(cwd):
        result.report = (
            f"refusing to treat runtime/global path as source: {cwd}\n"
            f"set HERMES_SOURCE_ROOT or use {DEFAULT_HARNESS_SOURCE}"
        )
        return result

    current_branch = _git_out(git_cmd, cwd, ["rev-parse", "--abbrev-ref", "HEAD"]) or "HEAD"
    pre_head = _git_out(git_cmd, cwd, ["rev-parse", "HEAD"])
    result.pre_head = pre_head
    result.branch = current_branch if current_branch != "HEAD" else branch
    dirty = _git_out(git_cmd, cwd, ["status", "--porcelain"])

    paused_rebase = _paused_rebase(git_cmd, cwd)
    if paused_rebase or _unmerged_index(git_cmd, cwd):
        conflicted = [
            line
            for line in _git_out(git_cmd, cwd, ["diff", "--name-only", "--diff-filter=U"]).splitlines()
            if line.strip()
        ]
        post_head = _git_out(git_cmd, cwd, ["rev-parse", "HEAD"])
        status_text = _git_out(git_cmd, cwd, ["status"])
        body = "\n".join(
            [
                f"source={cwd}",
                f"branch={result.branch}",
                f"head={pre_head}",
                "conflicted_files:",
                *([f"  {name}" for name in conflicted] or ["  (none listed)"]),
                "git_status:",
                status_text or "",
            ]
        )
        diagnostic = _write_diagnostic(runtime_home, "conflict", body)
        prior = _read_json(runtime_home / RECOVERY_NAME)
        recovery = {
            "status": "committed-rebase-conflict" if paused_rebase else "stash-pop-conflict",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source_root": str(cwd),
            "runtime_home": str(runtime_home),
            "branch": result.branch,
            "head": pre_head,
            "origin_url": _git_out(git_cmd, cwd, ["remote", "get-url", "origin"]),
            "upstream_url": _git_out(git_cmd, cwd, ["remote", "get-url", "upstream"]),
            "origin_head": _git_out(git_cmd, cwd, ["rev-parse", f"origin/{result.branch}"]),
            "upstream_head": _git_out(git_cmd, cwd, ["rev-parse", f"upstream/{result.branch}"]),
            "stash_ref": _git_out(git_cmd, cwd, ["rev-parse", "-q", "--verify", "refs/stash"]),
            "dirty": dirty,
            "post_head": post_head,
            "diagnostic_path": str(diagnostic),
            "conflicted_files": conflicted,
        }
        for key in ("origin_url", "upstream_url", "origin_head", "upstream_head", "stash_ref"):
            if not recovery[key] and prior.get(key):
                recovery[key] = prior[key]
        recovery_path = _record_recovery(runtime_home, recovery)
        result.status = "conflict"
        result.exit_code = CONFLICT_EXIT
        result.post_head = post_head
        result.recovery_path = str(recovery_path)
        result.diagnostic_path = str(diagnostic)
        result.conflicted_files = conflicted
        if paused_rebase:
            result.report = (
                "an earlier update left a rebase paused in this checkout; refusing to classify it as up to date.\n"
                "Resolve and continue:\n"
                f"  cd {cwd}\n"
                "  git status\n"
                "  git add <resolved-files>\n"
                "  git rebase --continue\n"
                "  hermes update\n"
                "Abort and restore the original HEAD and dirty worktree:\n"
                "  git rebase --abort\n"
                f"recovery={recovery_path}\n"
                f"diagnostic={diagnostic}"
            )
        else:
            if recovery.get("stash_ref"):
                resolution = (
                    "The stash is preserved; resolve the conflict markers in the tree, then:\n"
                    "  git stash drop\n"
                )
            else:
                resolution = "Resolve the conflict markers in the tree, then rerun:\n  hermes update\n"
            result.report = (
                "an earlier update left conflicted changes in the index; refusing to classify this checkout as up to date.\n"
                + resolution
                + f"recovery={recovery_path}\n"
                f"diagnostic={diagnostic}"
            )
        return result

    origin_url = _git_out(git_cmd, cwd, ["remote", "get-url", "origin"])
    ok_upstream, upstream_url = _ensure_upstream_remote(git_cmd, cwd)
    if not ok_upstream:
        result.report = f"failed to add upstream remote: {upstream_url}"
        return result

    fetch_origin = _git(git_cmd, cwd, ["fetch", "origin", branch])
    fetch_upstream = _git(git_cmd, cwd, ["fetch", "upstream", branch])
    if fetch_origin.returncode != 0:
        result.report = (
            "failed to fetch origin "
            f"{branch}: {fetch_origin.stderr.strip() or fetch_origin.stdout.strip()}"
        )
        return result
    if fetch_upstream.returncode != 0:
        result.report = (
            "failed to fetch upstream "
            f"{branch}: {fetch_upstream.stderr.strip() or fetch_upstream.stdout.strip()}"
        )
        return result

    origin_head = _git_out(git_cmd, cwd, ["rev-parse", f"origin/{branch}"])
    upstream_head = _git_out(git_cmd, cwd, ["rev-parse", f"upstream/{branch}"])
    if not upstream_head:
        result.report = f"upstream/{branch} missing after fetch"
        return result

    upstream_ahead = _git_out(git_cmd, cwd, ["rev-list", "--count", f"HEAD..upstream/{branch}"])
    fork_ahead = _git_out(git_cmd, cwd, ["rev-list", "--count", f"upstream/{branch}..HEAD"])
    try:
        fork_commit_count = int(fork_ahead or "0")
    except ValueError:
        fork_commit_count = 0


    if check_only:
        result.status = "up_to_date"
        result.exit_code = 0
        result.post_head = pre_head
        result.report = (
            f"source={cwd}\n"
            f"head={pre_head}\n"
            f"origin={origin_head}\n"
            f"upstream={upstream_head}\n"
            f"upstream_ahead={upstream_ahead or '0'}\n"
            f"fork_ahead={fork_ahead or '0'}\n"
            f"runtime={runtime_home}\n"
        )
        return result

    if upstream_ahead in {"", "0"}:
        result.status = "up_to_date"
        result.exit_code = 0
        result.post_head = pre_head
        recovery = {
            "status": "up-to-date",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source_root": str(cwd),
            "runtime_home": str(runtime_home),
            "branch": result.branch,
            "head": pre_head,
            "origin_url": origin_url,
            "upstream_url": upstream_url,
            "origin_head": origin_head,
            "upstream_head": upstream_head,
            "stash_ref": "",
            "dirty": dirty,
        }
        _record_recovery(runtime_home, recovery)
        result.report = f"up to date with upstream/{branch} (HEAD {pre_head[:12]})"
        return result

    stash_ref = ""
    if dirty and fork_commit_count == 0:
        stash = _git(
            git_cmd,
            cwd,
            ["stash", "push", "-u", "-m", "hermes-fork-update: worktree delta"],
        )
        if stash.returncode != 0:
            result.report = f"failed to stash dirty tree: {stash.stderr.strip()}"
            return result
        stash_ref = _git_out(git_cmd, cwd, ["rev-parse", "-q", "--verify", "refs/stash"])

    recovery = {
        "status": "in-progress",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(cwd),
        "runtime_home": str(runtime_home),
        "branch": result.branch,
        "head": pre_head,
        "origin_url": origin_url,
        "upstream_url": upstream_url,
        "origin_head": origin_head,
        "upstream_head": upstream_head,
        "stash_ref": stash_ref,
        "dirty": dirty,
    }
    recovery_path = _record_recovery(runtime_home, recovery)
    result.recovery_path = str(recovery_path)

    if fork_commit_count > 0:
        rebase = _git(git_cmd, cwd, ["rebase", "--autostash", f"upstream/{branch}"])
        if rebase.returncode != 0:
            conflicted = [
                line
                for line in _git_out(git_cmd, cwd, ["diff", "--name-only", "--diff-filter=U"]).splitlines()
                if line.strip()
            ]
            status_text = _git_out(git_cmd, cwd, ["status"])
            body = "\n".join(
                [
                    f"source={cwd}",
                    f"branch={result.branch}",
                    f"pre_head={pre_head}",
                    f"upstream={upstream_head}",
                    f"origin={origin_head}",
                    "conflicted_files:",
                    *([f"  {name}" for name in conflicted] or ["  (none listed)"]),
                    "rebase_stdout:",
                    rebase.stdout or "",
                    "rebase_stderr:",
                    rebase.stderr or "",
                    "git_status:",
                    status_text or "",
                ]
            )
            diagnostic = _write_diagnostic(runtime_home, "conflict", body)
            recovery["status"] = "committed-rebase-conflict"
            recovery["post_head"] = _git_out(git_cmd, cwd, ["rev-parse", "HEAD"])
            recovery["diagnostic_path"] = str(diagnostic)
            recovery["conflicted_files"] = conflicted
            _record_recovery(runtime_home, recovery)
            result.status = "conflict"
            result.exit_code = CONFLICT_EXIT
            result.post_head = recovery["post_head"]
            result.diagnostic_path = str(diagnostic)
            result.conflicted_files = conflicted
            result.report = (
                f"committed fork changes conflicted while rebasing onto upstream/{branch}; "
                "rebase paused with Git autostash active.\n"
                "Resolve and continue:\n"
                f"  cd {cwd}\n"
                "  git status\n"
                "  git add <resolved-files>\n"
                "  git rebase --continue\n"
                "  hermes update\n"
                "Abort and restore the original HEAD and dirty worktree:\n"
                "  git rebase --abort\n"
                f"recovery={recovery_path}\n"
                f"diagnostic={diagnostic}"
            )
            return result
    else:
        reset = _git(git_cmd, cwd, ["reset", "--hard", f"upstream/{branch}"])
        if reset.returncode != 0:
            result.report = f"failed to move {branch} to upstream/{branch}: {reset.stderr.strip()}"
            return result

    post_head = _git_out(git_cmd, cwd, ["rev-parse", "HEAD"])

    if stash_ref:
        popped = _git(git_cmd, cwd, ["stash", "pop"])
        if popped.returncode != 0:
            conflicted = [
                line
                for line in _git_out(git_cmd, cwd, ["diff", "--name-only", "--diff-filter=U"]).splitlines()
                if line.strip()
            ]
            status_text = _git_out(git_cmd, cwd, ["status"])
            body = "\n".join(
                [
                    f"source={cwd}",
                    f"branch={result.branch}",
                    f"pre_head={pre_head}",
                    f"upstream={upstream_head}",
                    f"origin={origin_head}",
                    "conflicted_files:",
                    *([f"  {name}" for name in conflicted] or ["  (none listed)"]),
                    "stash_pop_stdout:",
                    popped.stdout or "",
                    "stash_pop_stderr:",
                    popped.stderr or "",
                    "git_status:",
                    status_text or "",
                    "stash_ref:",
                    stash_ref,
                    "resolve the conflicts, then run: git stash drop",
                ]
            )
            diagnostic = _write_diagnostic(runtime_home, "conflict", body)
            recovery["status"] = "stash-pop-conflict"
            recovery["post_head"] = post_head
            recovery["diagnostic_path"] = str(diagnostic)
            recovery["conflicted_files"] = conflicted
            recovery["stash_ref"] = stash_ref
            _record_recovery(runtime_home, recovery)
            result.status = "conflict"
            result.exit_code = CONFLICT_EXIT
            result.post_head = post_head
            result.diagnostic_path = str(diagnostic)
            result.conflicted_files = conflicted
            result.report = (
                f"upstream moved to {post_head[:12]} and the uncommitted worktree delta conflicted on re-apply.\n"
                "The stash is preserved; resolve the conflict markers in the tree, then:\n"
                "  git stash drop\n"
                f"recovery={recovery_path}\n"
                f"diagnostic={diagnostic}"
            )
            return result

    result.status = "updated" if post_head != pre_head else "up_to_date"
    result.exit_code = 0
    result.post_head = post_head
    result.commits_replayed = fork_commit_count

    if post_head != pre_head:
        python = cwd / "venv" / "bin" / "python"
        configured_uv = os.environ.get("HERMES_UV", "").strip()
        managed_uv = runtime_home / "bin" / "uv"
        path_uv = shutil.which("uv")
        uv_candidates = [
            Path(configured_uv).expanduser() if configured_uv else None,
            managed_uv,
            Path(path_uv) if path_uv else None,
        ]
        uv = next((candidate for candidate in uv_candidates if candidate and candidate.is_file()), managed_uv)
        if not python.is_file() or not uv.is_file():
            diagnostic = _write_diagnostic(
                runtime_home,
                "runtime-refresh",
                "\n".join(
                    [
                        f"source={cwd}",
                        f"post_head={post_head}",
                        f"python={python}",
                        f"uv={uv}",
                        "rebase completed; source HEAD was not rolled back",
                    ]
                ),
            )
            recovery["status"] = "runtime-refresh-failed"
            recovery["post_head"] = post_head
            recovery["diagnostic_path"] = str(diagnostic)
            _record_recovery(runtime_home, recovery)
            result.status = "runtime-refresh-failed"
            result.exit_code = RUNTIME_REFRESH_EXIT
            result.post_head = post_head
            result.diagnostic_path = str(diagnostic)
            result.report = (
                "rebase completed, but editable runtime cannot be refreshed\n"
                f"diagnostic={diagnostic}"
            )
            return result
        refresh = subprocess.run(
            [
                str(uv),
                "pip",
                "install",
                "--python",
                str(python),
                "--editable",
                f"{cwd}[all]",
            ],
            cwd=cwd,
            env={**os.environ, "HERMES_HOME": str(runtime_home), "HERMES_SOURCE_ROOT": str(cwd)},
        )
        if refresh.returncode != 0:
            diagnostic = _write_diagnostic(
                runtime_home,
                "runtime-refresh",
                "\n".join(
                    [
                        f"source={cwd}",
                        f"post_head={post_head}",
                        f"python={python}",
                        f"uv={uv}",
                        f"exit_code={refresh.returncode}",
                        "rebase completed; source HEAD was not rolled back; uv output was streamed to the console",
                    ]
                ),
            )
            recovery["status"] = "runtime-refresh-failed"
            recovery["post_head"] = post_head
            recovery["diagnostic_path"] = str(diagnostic)
            _record_recovery(runtime_home, recovery)
            result.status = "runtime-refresh-failed"
            result.exit_code = RUNTIME_REFRESH_EXIT
            result.post_head = post_head
            result.diagnostic_path = str(diagnostic)
            result.report = (
                "rebase completed, but editable runtime refresh failed\n"
                f"diagnostic={diagnostic}"
            )
            return result
    recovery["status"] = "success"
    recovery["post_head"] = post_head
    recovery["commits_replayed"] = result.commits_replayed
    _record_recovery(runtime_home, recovery)
    result.report = (
        f"updated {result.branch} onto upstream/{branch}: "
        f"{pre_head[:12]} -> {post_head[:12]}" + ("; worktree delta re-applied" if stash_ref else "")
    )
    return result


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_pid(pid: int) -> None:
    if not _pid_alive(pid) or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(25):
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _serve_command(source: Path, host: str, port: int) -> list[str]:
    launcher = source / "scripts" / "hermes"
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return [str(launcher), "serve", "--host", host, "--port", str(port)]
    python = source / "venv" / "bin" / "python"
    if python.is_file() and os.access(python, os.X_OK):
        return [str(python), str(source / "hermes"), "serve", "--host", host, "--port", str(port)]
    runtime_python = _runtime_home() / "hermes-agent" / "venv" / "bin" / "python"
    if runtime_python.is_file() and os.access(runtime_python, os.X_OK):
        return [str(runtime_python), str(source / "hermes"), "serve", "--host", host, "--port", str(port)]
    return [str(source / "hermes"), "serve", "--host", host, "--port", str(port)]


def _spawn_serve(source: Path, host: str, port: int, token: str, detach: bool = True) -> subprocess.Popen:
    cmd = _serve_command(source, host, port)
    runtime_home = _runtime_home()
    log_dir = runtime_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "managed-backend.log", "ab")
    env = {
        **os.environ,
        "HERMES_DASHBOARD_SESSION_TOKEN": token,
        "HERMES_SOURCE_ROOT": str(source),
        "HERMES_HOME": str(runtime_home),
    }
    return subprocess.Popen(
        cmd,
        cwd=source,
        env=env,
        stdout=log_file,
        stderr=log_file,
        start_new_session=detach,
    )


def _write_ready_record(runtime_home: Path, source: Path, host: str, port: int, pid: int, status: str) -> None:
    _write_json(
        runtime_home / READY_RECORD_NAME,
        {
            "host": host,
            "port": port,
            "pid": pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "source_root": str(source),
            "status": status,
        },
    )


def _backend_version() -> str:
    try:
        from hermes_cli import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _healthy_backend(host: str, port: int) -> int:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return 0
    except OSError:
        return 1


def _backend_listener_pid(host: str, port: int) -> int:
    pid = 0
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["lsof", "-nP", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    text = line.strip()
                    if text and text.isdigit():
                        pid = int(text)
                        break
        except (OSError, ValueError, subprocess.SubprocessError):
            pid = 0
    if pid:
        return pid
    if _healthy_backend(host, port):
        return 0
    for name in (DESKTOP_READY_NAME, READY_RECORD_NAME):
        try:
            rec = int(_read_json(_runtime_home() / name).get("pid") or 0)
        except (TypeError, ValueError):
            rec = 0
        if rec:
            return rec
    return 0


def _mint_session_token() -> str:
    return secrets.token_urlsafe(32)


def _resolve_backend_endpoint(source: Path, runtime_home: Path) -> tuple[str, int]:
    return DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_PORT


def _write_desktop_record(
    runtime_home: Path,
    source: Path,
    host: str,
    port: int,
    pid: int,
    token: str,
) -> None:
    path = runtime_home / DESKTOP_READY_NAME
    old = _read_json(path)
    git_sha = _git_out(["git"], source, ["rev-parse", "HEAD"])
    try:
        generation = int(old.get("generation") or 0) + 1
    except (TypeError, ValueError):
        generation = 1
    _write_json(
        path,
        {
            "schemaVersion": int(old.get("schemaVersion") or 1),
            "kind": old.get("kind") or "hermes-serve-safehouse",
            "owner": old.get("owner") or "hermes-serve-safehouse",
            "host": host,
            "port": int(port),
            "pid": int(pid),
            "generation": generation,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "sourceRoot": str(source),
            "version": _backend_version(),
            "gitSha": git_sha,
            "healthPath": old.get("healthPath") or "/api/health",
            "token": token,
            "launchCommand": old.get("launchCommand") or "hermes-backend-start",
            "updateCommand": old.get("updateCommand") or "hermes update",
            "restartCommand": old.get("restartCommand") or "hermes-backend-restart",
        },
    )


def start_managed_backend(source_root: Optional[Path] = None, foreground: bool = False) -> int:
    source = resolve_source_root(source_root)
    runtime_home = _runtime_home()
    host, port = _resolve_backend_endpoint(source, runtime_home)
    existing = _backend_listener_pid(host, port)
    if existing:
        return existing
    token = _mint_session_token()
    proc = _spawn_serve(source, host, port, token, detach=not foreground)
    pid = proc.pid or 0
    listening = _wait_port(host, port)
    status = "running" if listening else "started-unchecked"
    _write_ready_record(runtime_home, source, host, port, pid, status)
    _write_desktop_record(runtime_home, source, host, port, pid, token)
    if proc.poll() is not None and not listening:
        raise RuntimeError(f"managed backend exited immediately with code {proc.returncode}")
    if foreground:
        while proc.poll() is None:
            time.sleep(2)
    return pid


def _serve_like_command(cmd: str) -> bool:
    return "serve" in cmd and str(DEFAULT_BACKEND_PORT) in cmd


def _serve_like_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and _serve_like_command(out.stdout)


def _stop_recorded_backend(runtime_home: Path) -> None:
    for name in (DESKTOP_READY_NAME, READY_RECORD_NAME):
        payload = _read_json(runtime_home / name)
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if _serve_like_pid(pid):
            _stop_pid(pid)


def restart_managed_backend(source_root: Optional[Path] = None) -> None:
    source = resolve_source_root(source_root)
    runtime_home = _runtime_home()
    host, port = DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_PORT
    _stop_recorded_backend(runtime_home)
    if sys.platform == "darwin":
        uid = os.getuid()
        kicked = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{DEFAULT_LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if kicked.returncode == 0:
            _write_ready_record(runtime_home, source, host, port, 0, "kickstarted")
            return
    token = _mint_session_token()
    proc = _spawn_serve(source, host, port, token)
    pid = proc.pid or 0
    listening = _wait_port(host, port)
    status = "running" if listening else "started-unchecked"
    _write_ready_record(runtime_home, source, host, port, pid, status)
    _write_desktop_record(runtime_home, source, host, port, pid, token)
    if proc.poll() is not None and not listening:
        raise RuntimeError(f"managed backend exited immediately with code {proc.returncode}")


def _safehouse_command() -> str:
    for candidate in (
        os.environ.get("SAFEHOUSE_BIN", ""),
        shutil.which("safehouse") or "",
        "/opt/homebrew/bin/safehouse",
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return "/opt/homebrew/bin/safehouse"


def _push_fork_to_origin() -> bool:
    source = resolve_source_root()
    origin_url = _git_out(["git"], source, ["remote", "get-url", "origin"]).lower()
    normalized = origin_url.replace("https://", "").replace("http://", "").replace("git@", "").replace(":", "/")
    if "nousresearch/hermes-agent" in normalized:
        return True
    _git(["git"], source, ["fetch", "origin", "main"])
    origin_sha = _git_out(["git"], source, ["rev-parse", "origin/main"])
    lease = f"--force-with-lease=refs/heads/main:{origin_sha}" if origin_sha else "--force-with-lease"
    pushed = _git(["git"], source, ["push", lease, "origin", "main"])
    if pushed.returncode == 0:
        return True
    fast = _git(["git"], source, ["push", "origin", "main"])
    return fast.returncode == 0


def _rebuild_desktop(source_root: Optional[Path] = None) -> None:
    source = resolve_source_root(source_root)
    python = source / "venv" / "bin" / "python"
    if not python.is_file():
        python = source / "venv" / "bin" / "python3"
    result = subprocess.run(
        [str(python), str(source / "hermes"), "desktop", "--force-build", "--build-only"],
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "HERMES_SOURCE_ROOT": str(source), "HERMES_HOME": str(_runtime_home())},
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError("desktop build failed: " + (tail[-1] if tail else "unknown error"))


def install_backend_launchd(source_root: Optional[Path] = None, home: Optional[Path] = None) -> Path:
    source = resolve_source_root(source_root)
    home = home or Path.home()
    plist_dir = home / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{DEFAULT_LAUNCHD_LABEL}.plist"
    composer_images_dir = home / "Library" / "Application Support" / "Hermes" / "composer-images"
    composer_images_dir.mkdir(parents=True, exist_ok=True)
    safehouse = _safehouse_command()
    watch = source / "scripts" / "hermes-backend-watch"
    args = [
        safehouse,
        "--workdir=",
        f"--add-dirs={home / 'Developer'}",
        f"--add-dirs={home / '.hermes'}",
        f"--add-dirs-ro={composer_images_dir}",
        f"--add-dirs-ro={home / '.local' / 'bin'}",
    ]
    profile = home / ".config" / "agent-safehouse" / "deny-sensitive-v2.sb"
    if profile.is_file():
        args.append(f"--append-profile={profile}")
    args.append(str(watch))
    log_dir = home / ".hermes" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": DEFAULT_LAUNCHD_LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(source),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(log_dir / "backend-launchd.out.log"),
        "StandardErrorPath": str(log_dir / "backend-launchd.err.log"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    plist_path.write_bytes(plistlib.dumps(payload))
    return plist_path


def load_backend_launchd(source_root: Optional[Path] = None, home: Optional[Path] = None) -> Path:
    plist_path = install_backend_launchd(source_root, home)
    loaded = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if loaded.returncode != 0 and "already loaded" not in (loaded.stderr + loaded.stdout).lower():
        raise RuntimeError(loaded.stderr.strip() or loaded.stdout.strip() or "launchctl bootstrap failed")
    return plist_path


def uninstall_backend_launchd(home: Optional[Path] = None) -> None:
    home = home or Path.home()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{DEFAULT_LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    plist_path = home / "Library" / "LaunchAgents" / f"{DEFAULT_LAUNCHD_LABEL}.plist"
    try:
        plist_path.unlink()
    except OSError:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-fork-update")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--start-backend", action="store_true")
    parser.add_argument("--restart-backend", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--install-backend-launchd", action="store_true")
    parser.add_argument("--uninstall-backend-launchd", action="store_true")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--no-desktop-build", action="store_true")
    args = parser.parse_args(argv)
    if args.install_backend_launchd:
        try:
            path = load_backend_launchd()
        except Exception as exc:
            print(f"backend launchd install failed: {exc}", file=sys.stderr)
            return ERROR_EXIT
        print(f"backend launchd installed: {path}")
        return 0
    if args.uninstall_backend_launchd:
        try:
            uninstall_backend_launchd()
        except Exception as exc:
            print(f"backend launchd uninstall failed: {exc}", file=sys.stderr)
            return ERROR_EXIT
        print("backend launchd uninstalled")
        return 0
    if args.restart_backend:
        try:
            restart_managed_backend()
        except Exception as exc:
            print(f"managed backend restart failed: {exc}", file=sys.stderr)
            return ERROR_EXIT
        print("managed backend restart requested")
        return 0
    if args.start_backend:
        try:
            pid = start_managed_backend(foreground=args.foreground)
        except Exception as exc:
            print(f"managed backend start failed: {exc}", file=sys.stderr)
            return ERROR_EXIT
        print(f"managed backend running (pid {pid})")
        return 0
    result = run_fork_aware_update(
        branch=args.branch,
        assume_yes=args.yes,
        check_only=args.check,
    )
    if result.report:
        print(result.report)
    if not args.check and result.exit_code == 0 and result.status in ("updated", "up_to_date"):
        try:
            if _push_fork_to_origin():
                print("→ pushed updated main to origin")
            else:
                print(
                    "⚠ origin push failed (update itself succeeded); push manually with: git push --force-with-lease origin main",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"⚠ origin push skipped: {exc}", file=sys.stderr)
        if result.status == "updated" and not args.no_desktop_build:
            try:
                _rebuild_desktop()
                print("→ desktop app rebuilt")
            except Exception as exc:
                print(f"⚠ desktop rebuild failed: {exc}", file=sys.stderr)
        if result.status == "updated":
            try:
                restart_managed_backend()
                print("→ managed backend restarted")
            except Exception as exc:
                print(f"⚠ managed backend restart failed: {exc}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
