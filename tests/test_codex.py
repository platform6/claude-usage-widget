"""Tests for the opt-in Codex provider (claude_usage.codex)."""

import os
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

import claude_usage.codex as codex
from claude_usage.codex import _clamp_expired, parse_rate_limits


FUTURE = int(time.time()) + 3600


def _payload(primary=None, secondary=None):
    limits = {}
    if primary is not None:
        limits["primary"] = primary
    if secondary is not None:
        limits["secondary"] = secondary
    return {"rateLimits": limits}


def test_parse_both_windows():
    parsed = parse_rate_limits(_payload(
        primary={"usedPercent": 54.2, "resetsAt": FUTURE, "windowDurationMins": 300},
        secondary={"usedPercent": 12.0, "resetsAt": FUTURE + 86400},
    ))
    assert parsed == {
        "session_pct": 0.542,
        "session_reset": FUTURE,
        "weekly_pct": 0.12,
        "weekly_reset": FUTURE + 86400,
    }


def test_parse_single_window_and_clamping():
    parsed = parse_rate_limits(_payload(primary={"usedPercent": 250.0, "resetsAt": None}))
    assert parsed is not None
    assert parsed["session_pct"] == 1.0  # clamped to 0..1
    assert parsed["session_reset"] == 0
    assert parsed["weekly_pct"] == 0.0


def test_parse_millisecond_resets_normalised():
    parsed = parse_rate_limits(_payload(primary={"usedPercent": 10, "resetsAt": FUTURE * 1000}))
    assert parsed is not None
    assert parsed["session_reset"] == FUTURE


def test_parse_rejects_unusable_payloads():
    assert parse_rate_limits(None) is None
    assert parse_rate_limits({}) is None
    assert parse_rate_limits({"rateLimits": "nope"}) is None
    assert parse_rate_limits(_payload()) is None
    assert parse_rate_limits(_payload(primary={"usedPercent": None})) is None
    assert parse_rate_limits(_payload(primary={"usedPercent": "NaN%"})) is None


def test_clamp_expired_windows_roll_back_to_zero():
    now = time.time()
    parsed = {
        "session_pct": 0.9, "session_reset": int(now - 60),
        "weekly_pct": 0.4, "weekly_reset": int(now + 3600),
    }
    out = _clamp_expired(parsed, now)
    assert out["session_pct"] == 0.0 and out["session_reset"] == 0
    assert out["weekly_pct"] == 0.4 and out["weekly_reset"] == int(now + 3600)


# --- collect_codex cache / throttle / fallback path -----------------------

def test_collect_codex_windows_polls_rpc():
    payload = _payload(primary={"usedPercent": 25, "resetsAt": FUTURE})
    with patch.object(codex.os, "name", "nt"), \
         patch.object(codex, "find_codex_bin", return_value="codex.exe"), \
         patch.object(codex, "_load_cache", return_value=None), \
         patch.object(codex, "_save_cache"), \
         patch.object(codex, "_rate_limits_rpc", return_value=payload) as rpc:
        out = codex.collect_codex()
    assert out["available"] is True
    assert out["session_pct"] == 0.25
    rpc.assert_called_once_with("codex.exe")


def test_collect_codex_missing_binary_is_unavailable():
    with patch.object(codex.os, "name", "posix"), \
         patch.object(codex, "find_codex_bin", return_value=None):
        out = codex.collect_codex()
    assert out["available"] is False
    assert "not found" in out["error"]


def test_collect_codex_serves_fresh_cache_without_spawning_rpc():
    fresh = {"fetched_at": time.time(), "payload": _payload(
        primary={"usedPercent": 20, "resetsAt": FUTURE},
        secondary={"usedPercent": 30, "resetsAt": FUTURE})}
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        return None

    with patch.object(codex.os, "name", "posix"), \
         patch.object(codex, "find_codex_bin", return_value="/usr/bin/codex"), \
         patch.object(codex, "_load_cache", return_value=fresh), \
         patch.object(codex, "_rate_limits_rpc", boom):
        out = codex.collect_codex(poll_seconds=300)
    assert calls["n"] == 0                     # fresh cache → no RPC spawn
    assert out["available"] is True
    assert abs(out["session_pct"] - 0.20) < 1e-6
    assert abs(out["weekly_pct"] - 0.30) < 1e-6


def test_collect_codex_rpc_success_saves_cache():
    payload = _payload(primary={"usedPercent": 55, "resetsAt": FUTURE},
                       secondary={"usedPercent": 5, "resetsAt": FUTURE})
    saved = {}
    with patch.object(codex.os, "name", "posix"), \
         patch.object(codex, "find_codex_bin", return_value="/usr/bin/codex"), \
         patch.object(codex, "_load_cache", return_value=None), \
         patch.object(codex, "_rate_limits_rpc", return_value=payload), \
         patch.object(codex, "_save_cache", side_effect=lambda p: saved.update(p=p)):
        out = codex.collect_codex()
    assert out["available"] is True
    assert abs(out["session_pct"] - 0.55) < 1e-6
    assert saved.get("p") == payload           # fresh result cached


def test_collect_codex_rpc_failure_falls_back_to_stale_cache():
    stale = {"fetched_at": time.time() - 99999, "payload": _payload(
        primary={"usedPercent": 42, "resetsAt": FUTURE})}
    with patch.object(codex.os, "name", "posix"), \
         patch.object(codex, "find_codex_bin", return_value="/usr/bin/codex"), \
         patch.object(codex, "_load_cache", return_value=stale), \
         patch.object(codex, "_rate_limits_rpc", return_value=None):
        out = codex.collect_codex(poll_seconds=300)
    assert out["available"] is True
    assert "rpc failed" in out["error"]
    assert abs(out["session_pct"] - 0.42) < 1e-6


@pytest.mark.parametrize("arch,package_arch,triple", [
    ("AMD64", "x64", "x86_64"), ("ARM64", "arm64", "aarch64"),
])
@pytest.mark.parametrize("layout", ["nested", "hoisted", "legacy"])
def test_windows_npm_discovery(tmp_path, monkeypatch, arch, package_arch, triple, layout):
    npm = tmp_path / "npm with spaces"
    package = npm / "node_modules" / "@openai" / "codex"
    if layout == "nested":
        root = package / "node_modules" / "@openai" / f"codex-win32-{package_arch}"
    elif layout == "hoisted":
        root = npm / "node_modules" / "@openai" / f"codex-win32-{package_arch}"
    else:
        root = package
    exe = root / "vendor" / f"{triple}-pc-windows-msvc" / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(codex, "_IS_WINDOWS", True)
    monkeypatch.setattr(codex.platform, "machine", lambda: arch)
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(npm / "codex.cmd"))
    assert codex.find_codex_bin() == str(exe)


def test_windows_discovery_without_npm_on_path(tmp_path, monkeypatch):
    exe = tmp_path / "npm/node_modules/@openai/codex/vendor/x86_64-pc-windows-msvc/codex/codex.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(codex, "_IS_WINDOWS", True)
    monkeypatch.setattr(codex.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert codex.find_codex_bin() == str(exe)


def test_windows_standalone_exe_and_unresolved_shim(tmp_path, monkeypatch):
    exe = tmp_path / "codex.exe"
    exe.touch()
    monkeypatch.setattr(codex, "_IS_WINDOWS", True)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(exe))
    assert codex.find_codex_bin() == str(exe)
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(tmp_path / "codex.cmd"))
    assert codex.find_codex_bin() is None


def _fake_server(monkeypatch, script):
    """Use real OS pipes and processes; only replace the command being launched."""
    popen = subprocess.Popen
    processes = []

    def launch(args, **kwargs):
        assert args == ["fake-codex", "app-server"]
        if os.name == "nt":
            assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        proc = popen([sys.executable, "-u", "-c", script], **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(codex.subprocess, "Popen", launch)
    return processes


def _assert_reaped(processes):
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed
    assert processes[0].stdout.closed


def test_rpc_real_pipes_handshake_and_fragmented_response(monkeypatch):
    payload = _payload(primary={"usedPercent": 37, "resetsAt": FUTURE})
    script = '''
import json, sys, time
assert json.loads(sys.stdin.readline())["method"] == "initialize"
print('noise\\n[]\\n{"method":"notification"}\\n{"id":1,"result":{}}', flush=True)
assert json.loads(sys.stdin.readline())["method"] == "initialized"
assert json.loads(sys.stdin.readline())["method"] == "account/rateLimits/read"
response = json.dumps({"id": 2, "result": PAYLOAD}) + "\\n"
sys.stdout.write(response[:12]); sys.stdout.flush()
time.sleep(0.05)
sys.stdout.write(response[12:]); sys.stdout.flush()
time.sleep(30)
'''.replace("PAYLOAD", repr(payload))
    processes = _fake_server(monkeypatch, script)
    assert codex._rate_limits_rpc("fake-codex", timeout=4) == payload
    _assert_reaped(processes)


@pytest.mark.parametrize("output", ["", '{"id":1', '{"id":1,"result":{}}\n'])
def test_rpc_timeout_kills_stalled_or_partial_server(monkeypatch, output):
    script = f"import sys,time; sys.stdout.write({output!r}); sys.stdout.flush(); time.sleep(30)"
    processes = _fake_server(monkeypatch, script)
    start = time.monotonic()
    assert codex._rate_limits_rpc("fake-codex", timeout=1) is None
    assert time.monotonic() - start < 3
    _assert_reaped(processes)


@pytest.mark.parametrize("script", [
    "pass",
    "print('{\"id\":1,\"error\":{\"code\":-1}}', flush=True)",
    "import sys; sys.stdin.readline(); print('{\"id\":1,\"result\":{}}', flush=True); "
    "sys.stdin.readline(); sys.stdin.readline(); print('{\"id\":2,\"error\":{\"code\":-1}}', flush=True)",
])
def test_rpc_eof_and_errors_return_without_waiting_for_deadline(monkeypatch, script):
    processes = _fake_server(monkeypatch, script)
    assert codex._rate_limits_rpc("fake-codex", timeout=4) is None
    _assert_reaped(processes)
