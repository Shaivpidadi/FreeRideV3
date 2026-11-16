"""``freeride daemon`` — keep the gateway running under the OS supervisor.

Ported from the ridex launcher. launchd (macOS) or a systemd user unit
(Linux) owns the process so a crash restarts it in seconds; with neither
available it falls back to a plain background process with a pid file.
``stop`` sticks: a marker file makes ``start`` the only way back, so an
agent auto-starting the gateway can't undo a deliberate stop.

State: ``~/.freeride/daemon/`` (pid, stop marker), log ``~/.freeride/daemon.log``
(created 0600: provider error text can carry request details).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import httpx

LAUNCHD_LABEL = "xyz.free-ride.gateway"
SYSTEMD_UNIT_NAME = "freeride.service"
DEFAULT_PORT = 11343


class Paths:
    def __init__(self, home: Path | None = None, port: int = DEFAULT_PORT) -> None:
        home = home or Path.home()
        self.port = port
        self.state_dir = home / ".freeride" / "daemon"
        self.stop_marker = self.state_dir / "stopped"
        self.pid_file = self.state_dir / "pid"
        self.log = home / ".freeride" / "daemon.log"
        self.plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        self.url = f"http://127.0.0.1:{port}"


def serve_argv(port: int) -> list[str]:
    """How the supervisor launches the gateway. ``freeride`` from PATH when
    installed (absolute, as launchd requires), else this interpreter."""
    exe = shutil.which("freeride")
    if exe:
        return [exe, "serve", "--port", str(port)]
    return [sys.executable, "-m", "freeride.cli.main", "serve", "--port", str(port)]


def supervisor_kind(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    if os.environ.get("FREERIDE_NO_SUPERVISOR") == "1":
        return "none"
    if platform.system() == "Darwin" and shutil.which("launchctl"):
        return "launchd"
    if shutil.which("systemctl"):
        try:
            if run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=5).returncode == 0:
                return "systemd"
        except (OSError, subprocess.SubprocessError):
            pass
    return "none"


def healthy(url: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _port_bound(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait(pred: Callable[[], bool], seconds: float, step: float = 0.3) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def render_plist(p: Paths) -> str:
    args = "".join(f"        <string>{a}</string>\n" for a in serve_argv(p.port))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}</string>
        <key>HOME</key><string>{Path.home()}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>StandardOutPath</key><string>{p.log}</string>
    <key>StandardErrorPath</key><string>{p.log}</string>
</dict>
</plist>
"""


def render_unit(p: Paths) -> str:
    exec_start = " ".join(serve_argv(p.port))
    return f"""[Unit]
Description=FreeRide gateway (local free-tier router)

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
StandardOutput=append:{p.log}
StandardError=append:{p.log}
UMask=0077

[Install]
WantedBy=default.target
"""


class Daemon:
    """All side effects go through ``run`` / ``popen`` so tests can fake them."""

    def __init__(self, paths: Paths | None = None, *, run=subprocess.run, popen=subprocess.Popen, kind: str | None = None) -> None:
        self.p = paths or Paths()
        self.run = run
        self.popen = popen
        self.kind = kind or supervisor_kind(run)

    # ----- helpers -----
    def _ensure_log(self) -> None:
        self.p.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.p.log.exists():
            fd = os.open(self.p.log, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            os.chmod(self.p.log, 0o600)
        except OSError:
            pass

    def _launchd(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(["launchctl", *args], capture_output=True, text=True, timeout=20)

    def _domain(self) -> str:
        uid = os.getuid() if hasattr(os, "getuid") else 0  # no getuid on Windows
        return f"gui/{uid}"

    def _spawn_plain(self) -> None:
        with open(self.p.log, "ab") as log:
            proc = self.popen(serve_argv(self.p.port), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        self.p.pid_file.write_text(str(proc.pid))

    def _supervisor_stop(self) -> None:
        if self.kind == "launchd":
            self._launchd("bootout", f"{self._domain()}/{LAUNCHD_LABEL}")
            _wait(lambda: self._launchd("print", f"{self._domain()}/{LAUNCHD_LABEL}").returncode != 0, 5)
        elif self.kind == "systemd":
            self.run(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME], capture_output=True, timeout=20)
            self.run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME], capture_output=True, timeout=20)

    def _kill_pid(self) -> None:
        try:
            pid = int(self.p.pid_file.read_text().strip())
            os.kill(pid, 15)
        except (OSError, ValueError):
            pass
        try:
            self.p.pid_file.unlink()
        except OSError:
            pass

    # ----- commands -----
    def spawn(self) -> str:
        self._ensure_log()
        if self.kind == "launchd":
            self._supervisor_stop()
            self.p.plist.parent.mkdir(parents=True, exist_ok=True)
            self.p.plist.write_text(render_plist(self.p))
            r = self._launchd("bootstrap", self._domain(), str(self.p.plist))
            if r.returncode != 0:
                self._spawn_plain()
                return "launchd bootstrap failed; started a plain background process instead"
            try:
                self.p.pid_file.unlink()
            except OSError:
                pass
            return "registered with launchd (KeepAlive)"
        if self.kind == "systemd":
            self.p.unit.parent.mkdir(parents=True, exist_ok=True)
            self.p.unit.write_text(render_unit(self.p))
            self.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=20)
            r = self.run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME], capture_output=True, timeout=20)
            if r.returncode != 0:
                self._spawn_plain()
                return "systemd user unit failed; started a plain background process instead"
            try:
                self.p.pid_file.unlink()
            except OSError:
                pass
            return "enabled systemd user unit (Restart=on-failure)"
        self._spawn_plain()
        return "started a plain background process (no supervisor available)"

    def start(self, *, wait_s: float = 30.0) -> tuple[bool, str]:
        self.p.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.p.stop_marker.unlink()
        except OSError:
            pass
        if healthy(self.p.url):
            return True, f"gateway already healthy on {self.p.url}"
        how = self.spawn()
        if _wait(lambda: healthy(self.p.url), wait_s):
            return True, f"gateway healthy on {self.p.url}; {how}"
        return False, f"gateway did not become healthy on {self.p.url} ({how}); see {self.p.log}"

    def stop(self) -> str:
        self.p.state_dir.mkdir(parents=True, exist_ok=True)
        self.p.stop_marker.touch()
        self._supervisor_stop()
        self._kill_pid()
        _wait(lambda: not _port_bound(self.p.port), 10)
        return "gateway stopped; it stays stopped until `freeride daemon start`"

    def restart(self) -> tuple[bool, str]:
        self.stop()
        return self.start()

    def uninstall(self) -> str:
        self.stop()
        removed = []
        for f in (self.p.plist, self.p.unit):
            try:
                f.unlink()
                removed.append(str(f))
            except OSError:
                pass
        if self.kind == "systemd":
            self.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=20)
        try:
            self.p.stop_marker.unlink()
        except OSError:
            pass
        return "removed: " + (", ".join(removed) or "nothing registered")

    def status(self) -> dict:
        return {
            "url": self.p.url,
            "healthy": healthy(self.p.url),
            "supervision": self.kind,
            "registered": self.p.plist.exists() if self.kind == "launchd" else self.p.unit.exists() if self.kind == "systemd" else self.p.pid_file.exists(),
            "stopped_by_user": self.p.stop_marker.exists(),
            "log": str(self.p.log),
        }


def cmd_daemon(args) -> int:
    d = Daemon(Paths(port=args.port))
    action = args.action
    if action in ("install", "start"):
        ok, msg = d.start()
        print(f"freeride daemon: {msg}")
        return 0 if ok else 1
    if action == "stop":
        print(f"freeride daemon: {d.stop()}")
        return 0
    if action == "restart":
        ok, msg = d.restart()
        print(f"freeride daemon: {msg}")
        return 0 if ok else 1
    if action == "uninstall":
        print(f"freeride daemon: {d.uninstall()}")
        return 0
    st = d.status()
    state = "healthy" if st["healthy"] else ("stopped by user" if st["stopped_by_user"] else "not running")
    print(f"freeride daemon: {state} on {st['url']}")
    print(f"  supervision : {st['supervision']} ({'registered' if st['registered'] else 'not registered'})")
    print(f"  log         : {st['log']}")
    if not st["healthy"] and not st["stopped_by_user"]:
        print("  start with  : freeride daemon start")
    return 0 if st["healthy"] else 1
