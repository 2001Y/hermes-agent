"""Non-destructive state.db auto-compaction tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_state import SessionDB
from hermes_state_autocompact import AutoCompactError, _safe_remote_config, maybe_auto_compact


def _fragmented_store(path: Path) -> None:
    db = SessionDB(db_path=path)
    db.create_session("session-1", source="cli")
    db.append_message("session-1", role="user", content="keep this history")
    db.append_message("session-1", role="assistant", content="and this answer")
    db.end_session("session-1", "done")
    db.close()

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE compaction_test_filler(value TEXT)")
    connection.executemany(
        "INSERT INTO compaction_test_filler(value) VALUES (?)",
        [("x" * 1000,)] * 2000,
    )
    connection.commit()
    connection.execute("DELETE FROM compaction_test_filler")
    connection.commit()
    connection.close()


def test_auto_compact_preserves_history_with_local_external_scratch(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _fragmented_store(db_path)

    result = maybe_auto_compact(
        db_path,
        raw_settings={
            "enabled": True,
            "min_interval_days": 0,
            "min_freelist_ratio": 0,
            "external_storage": {"type": "local", "path": str(tmp_path / "external")},
        },
    )

    assert result["compacted"] is True
    assert result["after_bytes"] < result["before_bytes"]
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT content FROM messages ORDER BY id").fetchall() == [
            ("keep this history",),
            ("and this answer",),
        ]
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT value FROM state_meta WHERE key = 'last_auto_compact'"
        ).fetchone() is not None
    finally:
        connection.close()
    assert list((tmp_path / "external").iterdir()) == []


def test_auto_compact_fails_closed_when_this_process_holds_state_db(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        result = maybe_auto_compact(
            db_path,
            raw_settings={"enabled": True, "min_interval_days": 0, "min_freelist_ratio": 0},
        )
    finally:
        db.close()

    assert result["compacted"] is False
    assert result["reason"] == "in_process_holder"


def test_ssh_storage_requires_a_safe_absolute_remote_directory():
    try:
        _safe_remote_config({"host": "ssh.2001y.dev", "remote_dir": "/home/y20010920t/hermes"})
    except AutoCompactError as exc:  # pragma: no cover - assertion below is the useful failure
        raise AssertionError(str(exc)) from exc

    for value in (
        {"host": "ssh.2001y.dev;bad", "remote_dir": "/home/hermes"},
        {"host": "ssh.2001y.dev", "remote_dir": "/home/../hermes"},
    ):
        try:
            _safe_remote_config(value)
        except AutoCompactError:
            continue
        raise AssertionError(f"unsafe SSH config was accepted: {value}")
