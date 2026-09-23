"""Non-destructive automatic compaction for ``state.db``.

The regular session retention sweep is intentionally separate from this module.  Compaction only
rewrites SQLite pages: it never selects or deletes sessions or messages.  The operation is performed
before a Hermes ``SessionDB`` is opened, while an exclusive SQLite guard is held, so promotion cannot
leave a live connection pointing at an old inode or an old WAL generation.

External storage is a scratch/backup target, not a live SQLite location.  The SSH backend transfers a
complete SQLite snapshot to a host-local directory, compacts it there, downloads the verified result,
and only then promotes it through SQLite's backup API.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from hermes_state_repair import (
    _copy_database_snapshot,
    _cross_process_repair_lock,
    _exclusive_repair_db_guard,
    _restore_journal_mode_after_repair,
)

logger = logging.getLogger("hermes_state")

_DEFAULT_MIN_INTERVAL_DAYS = 30
_DEFAULT_MIN_FREELIST_RATIO = 0.25
_MIN_FREE_BYTES = 256 * 1024 * 1024
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./+@:-]+$")
_REMOTE_COMPACTION_SCRIPT = r"""
import os
import shutil
import sqlite3
import sys

source, destination = sys.argv[1:3]
if os.path.exists(destination):
    os.unlink(destination)

connection = sqlite3.connect(source)
connection.isolation_level = None
try:
    connection.execute("BEGIN")
    for table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
        try:
            connection.execute(
                "INSERT INTO \"" + table + "\"(\"" + table + "\") VALUES('optimize')"
            )
        except sqlite3.Error:
            pass
    connection.execute("COMMIT")
    source_size = os.path.getsize(source)
    free = shutil.disk_usage(os.path.dirname(destination)).free
    if free < source_size * 2 + 256 * 1024 * 1024:
        raise RuntimeError(
            "remote scratch volume has insufficient free space for VACUUM INTO"
        )
    connection.execute("VACUUM INTO ?", (destination,))
finally:
    connection.close()

check = sqlite3.connect(destination)
try:
    result = check.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError("remote compacted database failed PRAGMA quick_check")
finally:
    check.close()
"""
_REMOTE_FINALIZE_SCRIPT = r"""
import os
import sys

incoming, final_source, compacted, keep_source = sys.argv[1:5]
if keep_source == "1":
    os.replace(incoming, final_source)
    os.chmod(final_source, 0o600)
else:
    for path in (incoming, final_source):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
try:
    os.unlink(compacted)
except FileNotFoundError:
    pass
"""


class AutoCompactError(RuntimeError):
    """A configured compaction backend could not produce a verified database."""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _storage_config(raw: Any) -> Tuple[str, Mapping[str, Any]]:
    """Normalize the optional storage block without accepting an ambiguous fallback."""
    if raw in (None, False, ""):
        return "local", {}
    if isinstance(raw, str):
        return "local", {"path": raw}
    if not isinstance(raw, Mapping):
        raise AutoCompactError("sessions.auto_compact.external_storage must be a mapping or path")
    kind = str(raw.get("type", "local")).strip().lower()
    if kind not in {"local", "ssh"}:
        raise AutoCompactError("external_storage.type must be 'local' or 'ssh'")
    return kind, raw


def _safe_remote_config(raw: Mapping[str, Any]) -> Tuple[str, Path, int, bool]:
    host = str(raw.get("host", "")).strip()
    remote_dir = str(raw.get("remote_dir", "")).strip()
    if not host or not _SSH_HOST_RE.fullmatch(host):
        raise AutoCompactError("external_storage.host must be a simple SSH host or SSH alias")
    if not _REMOTE_PATH_RE.fullmatch(remote_dir) or any(part in {".", ".."} for part in remote_dir.split("/")):
        raise AutoCompactError("external_storage.remote_dir must be an absolute path without '..'")
    try:
        port = int(raw.get("port", 22))
    except (TypeError, ValueError) as exc:
        raise AutoCompactError("external_storage.port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise AutoCompactError("external_storage.port must be between 1 and 65535")
    return host, Path(remote_dir), port, bool(raw.get("keep_source", True))


def _ssh_base(host: str, port: int) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", "-p", str(port), host]


def _run_process(command: list[str], *, input_text: Optional[str] = None, timeout: float = 7200.0) -> None:
    try:
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutoCompactError(f"external compaction command failed: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "command returned a non-zero status").strip()
        raise AutoCompactError(f"external compaction command failed: {detail[-1000:]}")


def _remote_command(host: str, port: int, program: str, args: Tuple[Path, ...]) -> list[str]:
    command = " ".join([program, *[shlex.quote(str(arg)) for arg in args]])
    return [*_ssh_base(host, port), command]


def _remote_mkdir(host: str, port: int, remote_dir: Path) -> None:
    quoted = shlex.quote(str(remote_dir))
    command = f"umask 077 && mkdir -p -- {quoted} && chmod 700 -- {quoted}"
    _run_process([*_ssh_base(host, port), command])


def _remote_compact(
    source: Path,
    destination: Path,
    *,
    host: str,
    remote_dir: Path,
    port: int,
    keep_source: bool,
    timeout: float,
) -> Tuple[str, Path, int, bool]:
    if shutil.which("ssh") is None or shutil.which("rsync") is None:
        raise AutoCompactError("SSH external compaction requires both ssh and rsync")
    _remote_mkdir(host, port, remote_dir)
    incoming = remote_dir / "source.state.db.incoming"
    compacted = remote_dir / "compact.state.db.incoming"
    remote_source = f"{host}:{incoming}"
    remote_compacted = f"{host}:{compacted}"
    cleanup = (
        f"rm -f -- {shlex.quote(str(incoming))} {shlex.quote(str(compacted))}"
    )
    _run_process([*_ssh_base(host, port), cleanup], timeout=timeout)

    _run_process(
        [
            "rsync", "-a", "--chmod=Fu=rw,Fgo=", "-e",
            f"ssh -o BatchMode=yes -o ConnectTimeout=30 -p {port}", str(source), remote_source,
        ],
        timeout=timeout,
    )
    _run_process(
        _remote_command(host, port, "python3 -", (incoming, compacted)),
        input_text=_REMOTE_COMPACTION_SCRIPT,
        timeout=timeout,
    )
    _run_process(
        [
            "rsync", "-a", "--chmod=Fu=rw,Fgo=", "-e",
            f"ssh -o BatchMode=yes -o ConnectTimeout=30 -p {port}", remote_compacted, str(destination),
        ],
        timeout=timeout,
    )
    # The caller finalizes only after it has verified and promoted the downloaded result.  If promotion
    # fails, the incoming files remain on the remote host for diagnosis and the next attempt safely
    # overwrites them.
    return host, remote_dir, port, keep_source


def _remote_finalize(
    *, host: str, remote_dir: Path, port: int, keep_source: bool, timeout: float
) -> None:
    incoming = remote_dir / "source.state.db.incoming"
    compacted = remote_dir / "compact.state.db.incoming"
    final_source = remote_dir / "source.state.db"
    finalize_args = (incoming, final_source, compacted, Path("1" if keep_source else "0"))
    _run_process(
        _remote_command(host, port, "python3 -", finalize_args),
        input_text=_REMOTE_FINALIZE_SCRIPT,
        timeout=timeout,
    )


def _local_compact(source: Path, destination: Path) -> None:
    connection = sqlite3.connect(source)
    connection.isolation_level = None
    try:
        connection.execute("BEGIN")
        for table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
            try:
                connection.execute(
                    f'INSERT INTO "{table}"("{table}") VALUES(\'optimize\')'
                )
            except sqlite3.Error:
                pass
        connection.execute("COMMIT")
        connection.execute("VACUUM INTO ?", (str(destination),))
    finally:
        connection.close()


def _canonical_counts(connection: sqlite3.Connection) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for table in ("sessions", "messages"):
        try:
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error as exc:
            raise AutoCompactError(f"cannot verify canonical table {table}: {exc}") from exc
    return counts


def _verify_compacted(path: Path, expected: Dict[str, int]) -> Dict[str, Any]:
    if not path.exists() or path.stat().st_size <= 0:
        raise AutoCompactError(f"compacted database was not produced at {path}")
    connection = sqlite3.connect(path)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise AutoCompactError(f"compacted database failed PRAGMA quick_check: {quick_check}")
        counts = _canonical_counts(connection)
        if counts != expected:
            raise AutoCompactError(
                f"compacted database changed canonical row counts: expected {expected}, got {counts}"
            )
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        return {
            "bytes": path.stat().st_size,
            "page_size": page_size,
            "page_count": page_count,
            "counts": counts,
        }
    finally:
        connection.close()


def _config() -> Mapping[str, Any]:
    from hermes_cli.config import load_config_readonly

    return _as_mapping(load_config_readonly().get("sessions"))


def _settings(raw: Any) -> Mapping[str, Any]:
    if raw is True:
        return {"enabled": True}
    if not isinstance(raw, Mapping):
        return {}
    return raw


def _path_size(connection: sqlite3.Connection) -> int:
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    return max(page_size * page_count, 1)


def maybe_auto_compact(
    db_path: Path,
    *,
    raw_settings: Any = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Run one safe, non-destructive compaction pass before ``db_path`` is opened by Hermes.

    ``raw_settings`` is injectable for tests and callers that already loaded the profile config.  A
    normal automatic pass is interval- and freelist-gated.  ``force`` bypasses both gates but still
    requires the holder/exclusive-lock and row-preservation checks.
    """
    db_path = Path(db_path).expanduser().resolve()
    settings = _settings(_config().get("auto_compact") if raw_settings is None else raw_settings)
    result: Dict[str, Any] = {
        "skipped": False,
        "compacted": False,
        "reason": None,
        "before_bytes": None,
        "after_bytes": None,
    }
    if not force and not bool(settings.get("enabled", False)):
        result.update(skipped=True, reason="disabled")
        return result
    if not db_path.exists():
        result.update(skipped=True, reason="database_missing")
        return result
    try:
        from hermes_cli.sqlite_safe_read import has_live_connection

        if has_live_connection(db_path):
            result.update(skipped=True, reason="in_process_holder")
            return result
    except Exception as exc:
        result.update(skipped=True, reason=f"holder_probe_failed: {exc}")
        return result
    try:
        min_interval_days = float(settings.get("min_interval_days", _DEFAULT_MIN_INTERVAL_DAYS))
        min_ratio = float(settings.get("min_freelist_ratio", _DEFAULT_MIN_FREELIST_RATIO))
    except (TypeError, ValueError) as exc:
        result.update(skipped=True, reason=f"invalid_settings: {exc}")
        return result
    if min_interval_days < 0 or not 0 <= min_ratio <= 1:
        result.update(skipped=True, reason="invalid_settings_range")
        return result
    try:
        storage_type, storage = _storage_config(settings.get("external_storage"))
        timeout = float(storage.get("timeout_seconds", 7200))
        if timeout <= 0:
            raise AutoCompactError("external_storage.timeout_seconds must be positive")
        if storage_type == "ssh":
            host, remote_dir, port, keep_source = _safe_remote_config(storage)
            scratch_root = db_path.parent
        else:
            host = remote_dir = port = keep_source = None
            scratch_root = Path(storage.get("path", db_path.parent)).expanduser().resolve()
            if scratch_root == Path("/") or scratch_root == db_path:
                raise AutoCompactError("external_storage.path is too broad or points at state.db")
            scratch_root.mkdir(parents=True, exist_ok=True)
            if not scratch_root.is_dir():
                raise AutoCompactError(f"external_storage.path is not a directory: {scratch_root}")
    except (OSError, AutoCompactError) as exc:
        result.update(skipped=True, reason=f"invalid_storage: {exc}")
        return result

    # The repair lock also serializes this with schema repair and other full-file rewrites.  The exclusive
    # SQLite guard remains held during remote work: releasing it would allow a write that the compacted
    # snapshot could silently overwrite at promotion time.
    remote_finalize: Optional[Tuple[str, Path, int, bool]] = None
    with _cross_process_repair_lock(db_path) as locked:
        if not locked:
            result.update(skipped=True, reason="maintenance_lock_held")
            return result
        with _exclusive_repair_db_guard(db_path) as (guard, guard_error):
            if guard is None:
                result.update(skipped=True, reason=f"database_not_quiescent: {guard_error}")
                return result
            try:
                now = time.time()
                last = guard.execute(
                    "SELECT value FROM state_meta WHERE key = 'last_auto_compact'"
                ).fetchone()
                if not force and last is not None:
                    try:
                        if now - float(last[0]) < min_interval_days * 86400:
                            result.update(skipped=True, reason="interval")
                            return result
                    except (TypeError, ValueError):
                        pass
                before_bytes = _path_size(guard)
                result["before_bytes"] = before_bytes
                page_count = int(guard.execute("PRAGMA page_count").fetchone()[0])
                freelist_count = int(guard.execute("PRAGMA freelist_count").fetchone()[0])
                ratio = freelist_count / page_count if page_count else 0.0
                result["freelist_ratio"] = ratio
                if not force and ratio < min_ratio:
                    guard.execute(
                        "INSERT INTO state_meta(key, value) VALUES('last_auto_compact', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(now),),
                    )
                    guard.commit()
                    result.update(skipped=True, reason="freelist_below_threshold")
                    return result

                try:
                    free_bytes = shutil.disk_usage(scratch_root).free
                except OSError as exc:
                    raise AutoCompactError(f"could not determine scratch free space: {exc}") from exc
                # The local staging copy is always needed. A local target also needs a second full
                # image for VACUUM INTO; an SSH target keeps that second image on the remote volume.
                local_need = before_bytes * (1 if storage_type == "ssh" else 2) + _MIN_FREE_BYTES
                if free_bytes < local_need:
                    raise AutoCompactError(
                        f"only {free_bytes / (1024**3):.2f} GiB free in scratch storage; "
                        f"need about {local_need / (1024**3):.2f} GiB"
                    )

                journal_mode = str(guard.execute("PRAGMA journal_mode").fetchone()[0])
                with tempfile.TemporaryDirectory(prefix="hermes-auto-compact-", dir=str(scratch_root)) as work:
                    work_dir = Path(work)
                    source = work_dir / "source.state.db"
                    compacted = work_dir / "compacted.state.db"
                    _copy_database_snapshot(db_path, source, source_connection=guard)
                    expected = _canonical_counts(guard)
                    if storage_type == "ssh":
                        assert isinstance(host, str) and isinstance(remote_dir, Path)
                        assert isinstance(port, int) and isinstance(keep_source, bool)
                        remote_finalize = _remote_compact(
                            source,
                            compacted,
                            host=host,
                            remote_dir=remote_dir,
                            port=port,
                            keep_source=keep_source,
                            timeout=timeout,
                        )
                    else:
                        _local_compact(source, compacted)
                    compact_info = _verify_compacted(compacted, expected)
                    _copy_database_snapshot(compacted, db_path, destination_connection=guard)
                    _restore_journal_mode_after_repair(db_path, journal_mode, conn=guard)
                    after = _canonical_counts(guard)
                    if after != expected:
                        # The source image is retained until this final check, so a failed promotion can
                        # be rolled back without replacing the live inode.
                        _copy_database_snapshot(source, db_path, destination_connection=guard)
                        _restore_journal_mode_after_repair(db_path, journal_mode, conn=guard)
                        raise AutoCompactError(
                            f"promotion changed canonical row counts: expected {expected}, got {after}"
                        )
                    quick_check = str(guard.execute("PRAGMA quick_check").fetchone()[0])
                    if quick_check != "ok":
                        _copy_database_snapshot(source, db_path, destination_connection=guard)
                        _restore_journal_mode_after_repair(db_path, journal_mode, conn=guard)
                        raise AutoCompactError(f"promoted database failed PRAGMA quick_check: {quick_check}")
                    guard.execute(
                        "INSERT INTO state_meta(key, value) VALUES('last_auto_compact', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(now),),
                    )
                    guard.commit()
                    result.update(
                        compacted=True,
                        after_bytes=_path_size(guard),
                        after_page_count=compact_info["page_count"],
                    )
                    logger.info(
                        "state.db auto-compacted without deleting history: %.1f MB -> %.1f MB "
                        "(freelist %.1f%%, storage=%s)",
                        before_bytes / (1024 * 1024),
                        compact_info["bytes"] / (1024 * 1024),
                        ratio * 100,
                        storage_type,
                    )
            except (AutoCompactError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
                result.update(skipped=True, reason=f"failed: {exc}")
                logger.warning("state.db auto-compaction skipped: %s", exc)
                return result
    if result.get("compacted") and remote_finalize is not None:
        try:
            _remote_finalize(
                host=remote_finalize[0],
                remote_dir=remote_finalize[1],
                port=remote_finalize[2],
                keep_source=remote_finalize[3],
                timeout=timeout,
            )
        except AutoCompactError as exc:
            # Local promotion is already verified; leave the incoming remote files for the next run
            # rather than reporting a false data-loss failure.
            logger.warning("remote auto-compaction cleanup deferred: %s", exc)
    return result


def maybe_auto_compact_from_config(*, db_path: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
    """Load the active profile config and run :func:`maybe_auto_compact` best-effort."""
    try:
        from hermes_constants import get_hermes_home

        path = Path(db_path) if db_path is not None else Path(get_hermes_home()) / "state.db"
        return maybe_auto_compact(path, force=force)
    except Exception as exc:  # startup maintenance must never take Hermes down
        logger.warning("state.db auto-compaction skipped: %s", exc)
        return {"skipped": True, "compacted": False, "reason": f"failed: {exc}"}
