"""Durable suppression and circuit breaking for automated cron failures.

The scheduler is intentionally noisy in its local execution log, but chat
notifications are an incident surface.  This module keeps those concerns
separate:

* every failure occurrence increments a durable incident counter;
* only the first occurrence and a changed fingerprint claim a chat alert;
* a transition back to healthy claims one recovery alert;
* destination attempts have a small budget and a provider-rejection circuit.

The state file is profile-scoped under ``<HERMES_HOME>/state`` and is updated
with an inter-process file lock plus atomic replacement so the gateway ticker
and an external cron fire provider cannot claim the same alert concurrently.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Iterator, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - the fallback is for Windows only.
    fcntl = None

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

STATE_VERSION = 1
DEFAULT_MAX_ATTEMPTS_PER_WINDOW = 3
DEFAULT_WINDOW_SECONDS = 15 * 60
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = 30 * 60
STATE_FILENAME = "cron_failure_notifications.json"

_TIMESTAMP_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?|"
    r"\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2})\b"
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_DYNAMIC_ID_RE = re.compile(
    r"\b(?P<label>request|trace|run|execution|event)[_-]?id\s*[=:]\s*[^\s,;]+",
    re.IGNORECASE,
)
_COUNTER_RE = re.compile(
    r"\b(?P<label>attempt|retry|count|line)\s*[=:]?\s*\d+\b",
    re.IGNORECASE,
)
_HARD_REJECTION_RE = re.compile(
    r"message[_ -]?limit[_ -]?exceeded|rate[_ -]?limit|too many requests|http\s*429|\b429\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FailureDecision:
    """The result of claiming a failure incident notification."""

    notify: bool
    identity: str
    fingerprint: str
    normalized_error: str
    consecutive_count: int
    reason: str


@dataclass(frozen=True)
class RecoveryDecision:
    """The result of recording a healthy run after an incident."""

    notify: bool
    identity: str
    consecutive_count: int
    reason: str


def normalize_failure_text(error: str | None) -> str:
    """Remove run-specific noise while preserving the failure class.

    Status codes and stable paths/messages are intentionally retained.  Only
    timestamps, UUIDs, explicit request/run IDs, and retry counters are
    normalized; two materially different provider errors therefore remain
    different fingerprints.
    """

    text = str(error or "unknown error").replace("\x00", " ")
    text = _TIMESTAMP_RE.sub("<timestamp>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _DYNAMIC_ID_RE.sub(
        lambda match: f"{match.group('label').lower()}_id=<id>", text
    )
    text = _COUNTER_RE.sub(
        lambda match: f"{match.group('label').lower()}=<number>", text
    )
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text[:2000] or "unknown error"


def fingerprint_failure(error: str | None) -> str:
    """Return a stable, non-sensitive fingerprint for an error message."""

    normalized = normalize_failure_text(error)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def default_state_path() -> Path:
    """Return the active profile's durable failure-notification state path."""

    return get_hermes_home() / "state" / STATE_FILENAME


def _as_datetime(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str:
    return _as_datetime(value).isoformat(timespec="seconds")


def _epoch(value: datetime | None) -> float:
    return _as_datetime(value).timestamp()


def _new_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "incidents": {}, "transports": {}}


class CronFailureNotifier:
    """Persist and atomically claim automated failure notifications."""

    def __init__(
        self,
        state_path: Path | str | None = None,
        *,
        max_attempts_per_window: int = DEFAULT_MAX_ATTEMPTS_PER_WINDOW,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        circuit_cooldown_seconds: int = DEFAULT_CIRCUIT_COOLDOWN_SECONDS,
    ) -> None:
        self.state_path = Path(state_path or default_state_path()).expanduser()
        self.lock_path = self.state_path.with_name(f".{self.state_path.name}.lock")
        self.max_attempts_per_window = max(1, int(max_attempts_per_window))
        self.window_seconds = max(1, int(window_seconds))
        self.circuit_cooldown_seconds = max(1, int(circuit_cooldown_seconds))
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.state_path.parent, 0o700)
        except OSError:
            pass

        with self._thread_lock:
            with self.lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    state = self._read_unlocked()
                    yield state
                    self._write_unlocked(state)
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return _new_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring invalid cron failure state %s: %s", self.state_path, exc)
            return _new_state()
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            logger.warning("Ignoring unsupported cron failure state %s", self.state_path)
            return _new_state()
        if not isinstance(payload.get("incidents"), dict):
            payload["incidents"] = {}
        if not isinstance(payload.get("transports"), dict):
            payload["transports"] = {}
        return payload

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=str(self.state_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.state_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def record_failure(
        self,
        identity: str,
        error: str | None,
        *,
        now: datetime | None = None,
        notification_possible: bool = True,
    ) -> FailureDecision:
        """Record one failure and atomically claim at most one alert."""

        incident_id = str(identity or "unknown")
        normalized = normalize_failure_text(error)
        fingerprint = fingerprint_failure(normalized)
        seen_at = _iso(now)

        with self._locked_state() as state:
            incidents = state["incidents"]
            previous = incidents.get(incident_id)
            same_incident = bool(
                isinstance(previous, dict)
                and previous.get("health") == "failing"
                and previous.get("fingerprint") == fingerprint
            )
            previous_attempted = bool(
                isinstance(previous, dict)
                and previous.get("notification_attempted_at")
            )
            count = (
                int(previous.get("consecutive_count", 0) or 0) + 1
                if same_incident
                else 1
            )
            should_notify = bool(
                notification_possible
                and (not same_incident or not previous_attempted)
            )
            if same_incident:
                reason = "duplicate" if not should_notify else "first_delivery_available"
                incident = dict(previous)
                incident.update(
                    {
                        "last_seen_at": seen_at,
                        "normalized_error": normalized,
                        "consecutive_count": count,
                    }
                )
            else:
                reason = "changed" if previous and previous.get("health") == "failing" else "new"
                incident = {
                    "health": "failing",
                    "fingerprint": fingerprint,
                    "normalized_error": normalized,
                    "first_seen_at": seen_at,
                    "last_seen_at": seen_at,
                    "consecutive_count": count,
                    "notification_attempted_at": None,
                    "notification_delivered_at": None,
                    "last_delivery_error": None,
                    "recovered_at": None,
                    "recovery_notified_at": None,
                }
            if should_notify:
                incident["notification_attempted_at"] = seen_at
            incidents[incident_id] = incident

        return FailureDecision(
            notify=should_notify,
            identity=incident_id,
            fingerprint=fingerprint,
            normalized_error=normalized,
            consecutive_count=count,
            reason=reason,
        )

    def record_success(
        self,
        identity: str,
        *,
        now: datetime | None = None,
        notification_possible: bool = True,
    ) -> RecoveryDecision:
        """Record a healthy run and claim one recovery alert if appropriate."""

        incident_id = str(identity or "unknown")
        recovered_at = _iso(now)
        with self._locked_state() as state:
            incident = state["incidents"].get(incident_id)
            if not isinstance(incident, dict) or incident.get("health") != "failing":
                return RecoveryDecision(False, incident_id, 0, "already_healthy")
            count = int(incident.get("consecutive_count", 0) or 0)
            should_notify = bool(
                notification_possible and incident.get("notification_attempted_at")
            )
            incident["health"] = "healthy"
            incident["last_seen_at"] = recovered_at
            incident["recovered_at"] = recovered_at
            if should_notify:
                incident["recovery_notified_at"] = recovered_at
            state["incidents"][incident_id] = incident

        return RecoveryDecision(
            notify=should_notify,
            identity=incident_id,
            consecutive_count=count,
            reason="recovered" if should_notify else "recovered_local_only",
        )

    def record_delivery(
        self,
        identity: str,
        *,
        delivered: bool,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record the outcome of the already-claimed incident alert."""

        incident_id = str(identity or "unknown")
        delivered_at = _iso(now)
        with self._locked_state() as state:
            incident = state["incidents"].get(incident_id)
            if not isinstance(incident, dict):
                return
            incident["last_delivery_at"] = delivered_at
            incident["notification_delivered_at"] = delivered_at if delivered else None
            incident["last_delivery_error"] = (
                normalize_failure_text(error) if error else None
            )
            state["incidents"][incident_id] = incident

    def claim_failure_targets(
        self,
        platforms: Sequence[str],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Claim failure-notification budget for each currently open target.

        The budget is per platform, not global: a Slack circuit does not suppress
        a healthy Matrix or Telegram sibling in a ``deliver=all`` job.
        """

        timestamp = _epoch(now)
        allowed: list[str] = []
        seen: set[str] = set()
        with self._locked_state() as state:
            transports = state["transports"]
            for raw_platform in platforms:
                platform = str(raw_platform or "").strip().lower()
                if not platform or platform in seen:
                    continue
                seen.add(platform)
                transport = transports.get(platform)
                if not isinstance(transport, dict):
                    transport = {
                        "window_started_at": timestamp,
                        "attempt_count": 0,
                        "circuit_open_until": 0.0,
                        "last_error": None,
                    }
                window_started = float(transport.get("window_started_at", timestamp) or timestamp)
                if timestamp - window_started >= self.window_seconds:
                    transport["window_started_at"] = timestamp
                    transport["attempt_count"] = 0
                    transport["circuit_open_until"] = 0.0
                if float(transport.get("circuit_open_until", 0.0) or 0.0) > timestamp:
                    transports[platform] = transport
                    continue
                if int(transport.get("attempt_count", 0) or 0) >= self.max_attempts_per_window:
                    transport["circuit_open_until"] = timestamp + self.circuit_cooldown_seconds
                    transports[platform] = transport
                    continue
                transport["attempt_count"] = int(transport.get("attempt_count", 0) or 0) + 1
                transports[platform] = transport
                allowed.append(platform)
        return allowed

    def record_transport_failure(
        self,
        platform: str,
        error: str | None,
        *,
        now: datetime | None = None,
    ) -> None:
        """Open a destination circuit for provider rate/limit rejections."""

        name = str(platform or "").strip().lower()
        if not name:
            return
        timestamp = _epoch(now)
        normalized = normalize_failure_text(error)
        with self._locked_state() as state:
            transports = state["transports"]
            transport = transports.setdefault(
                name,
                {
                    "window_started_at": timestamp,
                    "attempt_count": 0,
                    "circuit_open_until": 0.0,
                    "last_error": None,
                },
            )
            transport["last_error"] = normalized
            if _HARD_REJECTION_RE.search(str(error or "")):
                transport["circuit_open_until"] = max(
                    float(transport.get("circuit_open_until", 0.0) or 0.0),
                    timestamp + self.circuit_cooldown_seconds,
                )
            transports[name] = transport

    def state_snapshot(self) -> dict[str, Any]:
        """Return a read-only-style copy useful for diagnostics and tests."""

        with self._locked_state() as state:
            return json.loads(json.dumps(state))
