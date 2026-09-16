"""Stay-awake inhibitors for agent turns.

The default backend prevents idle/system sleep for the duration of each agent
turn without requiring administrator privileges.  macOS can opt into the
stronger closed-display backend, which mirrors Amphetamine's Power Protect
mechanism and requires a pre-installed, narrowly scoped ``sudoers`` rule.

Usage::

    with StayAwake(enabled=True):
        # Agent loop runs here — machine won't sleep
        ...
    # Machine can sleep again

Config keys::

    agent.stay_awake: false              # default: false
    agent.stay_awake_mode: idle          # idle | closed-display (macOS)
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_PMSET = "/usr/bin/pmset"
_SUDO = "/usr/bin/sudo"
_STAY_AWAKE_MODES = frozenset({"idle", "closed-display"})


class StayAwake:
    """Prevent OS sleep/idle for the duration of the context block.

    ``mode="idle"`` is the portable, unprivileged path from PR #106401.
    ``mode="closed-display"`` is macOS-only and uses the same PowerManagement
    setting as Amphetamine's Power Protect.  It never prompts for a password
    from inside a turn: the caller must provision the exact NOPASSWD rule once.
    """

    def __init__(self, enabled: bool = False, *, mode: str = "idle") -> None:
        self._enabled = enabled
        self._mode = _normalize_mode(mode)
        self._process: Optional[subprocess.Popen] = None
        self._original_state = None  # Windows: previous ES_* flags
        self._sleep_disabled_before: Optional[int] = None
        self._sleep_disabled_owned = False

    # ── Context manager ──────────────────────────────────────────────────

    def __enter__(self) -> "StayAwake":
        if not self._enabled:
            return self
        try:
            system = platform.system()
            if system == "Darwin":
                if self._mode == "closed-display":
                    self._start_macos_closed_display()
                else:
                    self._start_macos()
            elif system == "Linux":
                self._start_linux()
            elif system == "Windows":
                self._start_windows()
            if self._process is not None or self._original_state is not None or self._sleep_disabled_owned:
                logger.info("Stay-awake inhibitor started (os=%s, mode=%s)", system, self._mode)
        except Exception as exc:
            logger.warning("Failed to start stay-awake inhibitor: %s", exc)
        return self

    def __exit__(self, *args: object) -> None:
        if not self._enabled:
            return
        try:
            if self._sleep_disabled_owned:
                self._stop_macos_closed_display()
            if self._process is not None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None
                logger.info("Stay-awake inhibitor stopped")
            if self._original_state is not None:
                self._stop_windows()
        except Exception as exc:
            logger.warning("Failed to stop stay-awake inhibitor: %s", exc)

    # ── OS-specific backends ─────────────────────────────────────────────

    def _start_macos(self) -> None:
        """``caffeinate -i``: prevent idle sleep; display may still dim."""
        self._process = subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _start_macos_closed_display(self) -> None:
        """Use macOS PowerManagement to keep a closed display session awake.

        ``pmset disablesleep`` is a persistent system-wide setting rather than
        a process assertion.  Snapshot the existing value, change it only when
        this scope owns the change, and restore it on exit.  The command is
        deliberately non-interactive: a turn must never hang waiting for a
        password or a TTY.  If the one-time NOPASSWD setup is absent, retain
        the useful idle-only inhibitor and make the loss of closed-display
        coverage explicit in the log.
        """
        try:
            before = _read_sleep_disabled()
            if before == 1:
                logger.info("macOS closed-display sleep prevention already active")
                return
            _set_sleep_disabled(1)
        except Exception as exc:
            logger.warning(
                "Closed-display sleep prevention is unavailable; falling back to idle-only "
                "protection (install the Hermes PowerManagement sudoers rule): %s",
                _describe_subprocess_error(exc),
            )
            self._start_macos()
            return
        self._sleep_disabled_before = before
        self._sleep_disabled_owned = True

    def _stop_macos_closed_display(self) -> None:
        """Restore the value observed before this scope changed PowerManagement."""
        before = self._sleep_disabled_before
        self._sleep_disabled_before = None
        self._sleep_disabled_owned = False
        if before is None:
            return
        try:
            current = _read_sleep_disabled()
            if current == 1:
                _set_sleep_disabled(before)
            elif current != before:
                logger.warning(
                    "Leaving macOS SleepDisabled=%s unchanged; it changed externally during "
                    "the Hermes turn (original=%s)",
                    current,
                    before,
                )
        except Exception as exc:
            logger.warning(
                "Could not restore macOS SleepDisabled=%s after the Hermes turn: %s",
                before,
                _describe_subprocess_error(exc),
            )

    def _start_linux(self) -> None:
        """systemd-inhibit: block sleep + idle while the agent runs.

        Falls back gracefully if systemd is not available (containers,
        WSL1, non-systemd distros) — the Popen will raise FileNotFoundError
        which is caught by __enter__.
        """
        self._process = subprocess.Popen(
            [
                "systemd-inhibit",
                "--what=sleep:idle",
                "--why=Hermes agent working",
                "--who=hermes",
                "sleep",
                "infinity",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _start_windows(self) -> None:
        """SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED).

        Stores the previous state so __exit__ can restore it.
        """
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        kernel32 = getattr(ctypes, "windll").kernel32
        self._original_state = kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )

    def _stop_windows(self) -> None:
        """Restore the previous thread execution state (clears our flags)."""
        import ctypes

        ES_CONTINUOUS = 0x80000000
        kernel32 = getattr(ctypes, "windll").kernel32
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self._original_state = None


def _normalize_mode(mode: object) -> str:
    value = str(mode or "idle").strip().lower()
    if value in _STAY_AWAKE_MODES:
        return value
    logger.warning("Unknown agent.stay_awake_mode=%r; using idle", mode)
    return "idle"


def _read_sleep_disabled() -> int:
    """Read the current macOS SleepDisabled setting without elevation."""
    result = subprocess.run(
        [_PMSET, "-g"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields and fields[0] == "SleepDisabled" and fields[-1] in {"0", "1"}:
            return int(fields[-1])
    raise RuntimeError("pmset -g did not report SleepDisabled")


def _set_sleep_disabled(value: int) -> None:
    """Set the macOS system-wide SleepDisabled setting without a TTY prompt."""
    if value not in {0, 1}:
        raise ValueError(f"SleepDisabled must be 0 or 1, got {value}")
    subprocess.run(
        [_SUDO, "-n", _PMSET, "-a", "disablesleep", str(value)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _describe_subprocess_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"command exited with status {exc.returncode}"
    return str(exc) or type(exc).__name__


# ── Process-wide refcounted scope ────────────────────────────────────────────
# One OS inhibitor per process, shared by concurrent turns (gateway sessions,
# subagent children): the first active turn starts it, the last one stops it.
# Refcounting matters because Windows SetThreadExecutionState is not nestable —
# a second StayAwake exiting would clear the first one's flags.

_lock = threading.Lock()
_count = 0
_inhibitor: Optional[StayAwake] = None


def _config_enabled() -> bool:
    """``agent.stay_awake`` from config.yaml (default False)."""
    try:
        from hermes_cli.config import load_config_readonly

        return bool((load_config_readonly().get("agent", {}) or {}).get("stay_awake", False))
    except Exception:
        return False


def _config_mode() -> str:
    """``agent.stay_awake_mode`` from config.yaml (default ``idle``)."""
    try:
        from hermes_cli.config import load_config_readonly

        agent_config = load_config_readonly().get("agent", {}) or {}
        return _normalize_mode(agent_config.get("stay_awake_mode", "idle"))
    except Exception:
        return "idle"


@contextmanager
def turn_scope(enabled: Optional[bool] = None) -> Iterator[None]:
    """Hold the shared stay-awake inhibitor for the duration of one agent turn.

    ``enabled=None`` reads ``agent.stay_awake`` from config; the whole scope is
    a no-op when disabled. Re-entrant across threads via refcount. The default
    ``idle`` mode is unprivileged; ``closed-display`` is macOS-only and requires
    a one-time exact-command NOPASSWD sudoers rule.
    """
    global _count, _inhibitor
    active = _config_enabled() if enabled is None else enabled
    if not active:
        yield
        return
    mode = _config_mode()
    with _lock:
        _count += 1
        if _count == 1:
            _inhibitor = StayAwake(enabled=True, mode=mode)
            _inhibitor.__enter__()
    try:
        yield
    finally:
        with _lock:
            _count -= 1
            if _count == 0 and _inhibitor is not None:
                _inhibitor.__exit__(None, None, None)
                _inhibitor = None
