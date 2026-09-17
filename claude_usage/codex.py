"""OpenAI Codex rate-limit collector (opt-in second provider).

Talks to the local ``codex`` CLI's ``app-server`` over stdio JSON-RPC:
``initialize`` -> ``initialized`` -> ``account/rateLimits/read``. The
response carries a ``rateLimits`` object with a ``primary`` (~5h) and
``secondary`` (weekly) window, each with ``usedPercent`` and ``resetsAt``
— the same shape as Claude's session/weekly pair, so the overlay can
render them with the exact same ring/bar primitives.

Spawning the app-server takes a couple of seconds, so results are cached
on disk (`~/.cache/claude-usage/codex_limits.json`) and only refreshed
every ``poll_seconds``. Between polls — and on RPC failure — the cache is
served, with expired windows clamped back to zero exactly like the Claude
sample-fallback path in ``collector.collect_all``.

Pipe reads use ``select`` on POSIX and ``PeekNamedPipe`` on Windows so a
stalled or partially written response cannot block the refresh thread.
"""

from __future__ import annotations

import json
import os
import platform
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

RPC_TIMEOUT_SECONDS = 12
DEFAULT_POLL_SECONDS = 300
CACHE_PATH = Path.home() / ".cache" / "claude-usage" / "codex_limits.json"
_BIN_CANDIDATES = ("/opt/homebrew/bin/codex", "/usr/local/bin/codex")
_IS_WINDOWS = os.name == "nt"


def _windows_codex_exe(candidate: str) -> str | None:
    """Resolve npm shims to the native binary; avoid leaving a shell/Node child."""
    path = Path(candidate)
    if path.suffix.lower() == ".exe" and path.is_file():
        return str(path)
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    package_arch = "arm64" if arch == "aarch64" else "x64"
    package = path.parent / "node_modules" / "@openai" / "codex"
    # Current optional platform package and older bundled-vendor installs.
    roots = (
        package / "node_modules" / "@openai" / f"codex-win32-{package_arch}",
        path.parent / "node_modules" / "@openai" / f"codex-win32-{package_arch}",
        package,
    )
    for root in roots:
        vendor = root / "vendor" / f"{arch}-pc-windows-msvc"
        for relative in ("bin/codex.exe", "codex/codex.exe"):
            exe = vendor / relative
            if exe.is_file():
                return str(exe)
    return None


def find_codex_bin() -> str | None:
    """Locate the ``codex`` CLI, preferring whatever is on PATH."""
    which = shutil.which("codex")
    if _IS_WINDOWS:
        if which:
            exe = _windows_codex_exe(which)
            if exe:
                return exe
        # GUI launches may not inherit npm's user-level PATH entry.
        appdata = os.environ.get("APPDATA")
        if appdata:
            return _windows_codex_exe(os.path.join(appdata, "npm", "codex.cmd"))
        return None
    if which:
        return which
    for candidate in _BIN_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _read_pipe(fd: int, timeout: float) -> bytes:
    """Read available bytes within timeout; b'' means timeout or EOF."""
    if not _IS_WINDOWS:
        ready, _, _ = select.select([fd], [], [], timeout)
        return os.read(fd, 65536) if ready else b""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                     ctypes.POINTER(wintypes.DWORD)]
    peek.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    available = wintypes.DWORD()
    deadline = time.monotonic() + timeout
    while True:
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232, 233):  # broken, closing, or disconnected pipe
                return b""
            raise ctypes.WinError(error)
        if available.value:
            return os.read(fd, min(available.value, 65536))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b""
        time.sleep(min(0.01, remaining))


def _rate_limits_rpc(codex_bin: str, timeout: float = RPC_TIMEOUT_SECONDS) -> dict[str, Any] | None:
    """Run one ``account/rateLimits/read`` round-trip against ``codex app-server``.

    The read side is hard-bounded by a wall-clock deadline using pipe polling +
    raw ``os.read`` rather than ``readline``: pipe readiness only
    guarantees at least one byte, so a blocking ``readline`` on a partial line
    with no trailing newline could block past the deadline and hang the refresh
    thread — which, via the widget's single-flight ``_refreshing`` latch, would
    freeze *every* subsequent refresh. Accumulating bytes and splitting on
    newlines ourselves keeps the whole call bounded at ``timeout`` seconds no
    matter how the app-server behaves.
    """
    proc = subprocess.Popen(
        [codex_bin, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
    )

    def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    result: dict[str, Any] | None = None
    buf = b""
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {
                "name": "claude-usage-widget",
                "title": "Claude Usage Widget",
                "version": "0",
            }},
        })
        deadline = time.monotonic() + timeout
        while result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = _read_pipe(fd, remaining)
            if not chunk:  # EOF — app-server exited
                break
            buf += chunk
            while b"\n" in buf and result is None:
                raw, buf = buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == 1:
                    if "error" in msg:
                        return None
                    send({"jsonrpc": "2.0", "method": "initialized"})
                    # The app-server needs a beat between the handshake and the
                    # first real request or it drops it on the floor.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.6:
                        return None
                    time.sleep(0.6)
                    send({"jsonrpc": "2.0", "id": 2,
                          "method": "account/rateLimits/read", "params": {}})
                elif msg.get("id") == 2:
                    return msg.get("result")
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)  # reap so we don't leave a zombie
        except Exception:
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
    return result


def parse_rate_limits(payload: Any) -> dict[str, Any] | None:
    """Extract the two utilization windows from a rateLimits/read result.

    Returns ``{"session_pct", "session_reset", "weekly_pct", "weekly_reset"}``
    (pct 0..1, reset as unix seconds, 0 when absent), or None when the
    payload has no usable window data.
    """
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rateLimits")
    if not isinstance(limits, dict):
        return None

    def window(block: Any) -> tuple[float, int] | None:
        if not isinstance(block, dict) or block.get("usedPercent") is None:
            return None
        try:
            pct = max(0.0, min(1.0, float(block["usedPercent"]) / 100.0))
        except (TypeError, ValueError):
            return None
        reset = block.get("resetsAt")
        try:
            reset_ts = int(reset) if reset is not None else 0
        except (TypeError, ValueError):
            reset_ts = 0
        if reset_ts > 10**12:  # milliseconds — normalise to seconds
            reset_ts //= 1000
        return pct, reset_ts

    primary = window(limits.get("primary"))
    secondary = window(limits.get("secondary"))
    if primary is None and secondary is None:
        return None
    return {
        "session_pct": primary[0] if primary else 0.0,
        "session_reset": primary[1] if primary else 0,
        "weekly_pct": secondary[0] if secondary else 0.0,
        "weekly_reset": secondary[1] if secondary else 0,
    }


def _load_cache() -> dict[str, Any] | None:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_cache(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "payload": payload}, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def _clamp_expired(parsed: dict[str, Any], now_ts: float) -> dict[str, Any]:
    """A window whose reset has passed has rolled over — show 0, not stale %."""
    out = dict(parsed)
    if out["session_reset"] and now_ts >= out["session_reset"]:
        out["session_pct"], out["session_reset"] = 0.0, 0
    if out["weekly_reset"] and now_ts >= out["weekly_reset"]:
        out["weekly_pct"], out["weekly_reset"] = 0.0, 0
    return out


def collect_codex(poll_seconds: int = DEFAULT_POLL_SECONDS) -> dict[str, Any]:
    """Return Codex utilization for the overlay; never raises.

    ``{"available": bool, "session_pct", "session_reset", "weekly_pct",
    "weekly_reset", "error": str}`` — available=False hides the Codex UI.
    """
    unavailable = {
        "available": False, "error": "",
        "session_pct": 0.0, "session_reset": 0,
        "weekly_pct": 0.0, "weekly_reset": 0,
    }
    codex_bin = find_codex_bin()
    if codex_bin is None:
        return {**unavailable, "error": "codex binary not found"}

    now_ts = time.time()
    cache = _load_cache()
    if cache is not None:
        age = now_ts - float(cache.get("fetched_at", 0) or 0)
        parsed = parse_rate_limits(cache.get("payload"))
        if parsed is not None and 0 <= age < poll_seconds:
            return {"available": True, "error": "", **_clamp_expired(parsed, now_ts)}

    payload = None
    try:
        payload = _rate_limits_rpc(codex_bin)
    except OSError:
        payload = None
    parsed = parse_rate_limits(payload)
    if parsed is not None:
        assert isinstance(payload, dict)
        _save_cache(payload)
        return {"available": True, "error": "", **_clamp_expired(parsed, now_ts)}

    # RPC failed — fall back to any cache, however old, before giving up.
    if cache is not None:
        parsed = parse_rate_limits(cache.get("payload"))
        if parsed is not None:
            return {"available": True, "error": "rpc failed; serving cache",
                    **_clamp_expired(parsed, now_ts)}
    return {**unavailable, "error": "rateLimits/read returned no window data"}
