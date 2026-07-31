"""Evidence-first local performance probes for Hermes.

This module intentionally does not claim to provide a universal benchmark score.
It supplies a common measurement envelope (run identity, wall time, process
resources, database size, and outcome) plus small workload adapters. Adapters
must be named explicitly so unlike workloads are never silently compared.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Sequence


SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path.home() / ".hermes" / "state.db"
_TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _numeric_fd(fd: str) -> int | None:
    match = re.match(r"^(\d+)", fd)
    return int(match.group(1)) if match else None


def parse_lsof_output(text: str) -> dict[str, Any]:
    """Summarize real descriptors, excluding mmap/cwd/mem rows from lsof."""
    type_counts: Counter[str] = Counter()
    descriptors: list[int] = []
    rows = 0
    for line in text.splitlines()[1:]:
        columns = line.split()
        if len(columns) < 5:
            continue
        rows += 1
        fd = columns[3]
        descriptor = _numeric_fd(fd)
        if descriptor is None:
            continue
        descriptors.append(descriptor)
        type_counts[columns[4]] += 1
    return {
        "lsof_rows": rows,
        "open_fd_count": len(descriptors),
        "highest_fd": max(descriptors) if descriptors else None,
        "fd_types": dict(sorted(type_counts.items())),
    }


def _lsof_snapshot(pid: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"lsof failed: {exc}"}
    if result.returncode != 0 and not result.stdout:
        return {"error": (result.stderr or "lsof failed").strip()}
    return parse_lsof_output(result.stdout)


def process_snapshot(pid: int | None) -> dict[str, Any]:
    if pid is None:
        return {}
    snapshot = {"pid": pid}
    snapshot.update(_lsof_snapshot(pid))
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=,threads=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        fields = result.stdout.split()
        if len(fields) >= 2:
            snapshot["rss_kib"] = int(fields[0])
            snapshot["threads"] = int(fields[1])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return snapshot


def resource_snapshot(
    *, pid: int | None = None, db_path: Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    usage = shutil.disk_usage(Path.home())
    snapshot: dict[str, Any] = {
        "captured_at": _utc_now(),
        "home_disk_free_bytes": usage.free,
        "home_disk_total_bytes": usage.total,
        "db_path": str(db_path),
    }
    try:
        snapshot["db_size_bytes"] = db_path.stat().st_size
    except OSError:
        snapshot["db_size_bytes"] = None
    snapshot.update(process_snapshot(pid))
    return snapshot


def make_record(
    *,
    workload: str,
    adapter: str,
    started_at: str,
    ended_at: str,
    outcome: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start = dt.datetime.fromisoformat(started_at)
    end = dt.datetime.fromisoformat(ended_at)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "workload": workload,
        "adapter": adapter,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": round((end - start).total_seconds() * 1000, 3),
        "outcome": outcome,
    }
    if before is not None:
        record["resources_before"] = before
    if after is not None:
        record["resources_after"] = after
    if metadata:
        record["metadata"] = metadata
    return record


def run_command(
    argv: Sequence[str],
    *,
    workload: str,
    adapter: str,
    pid: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run one bounded local command and record only safe metadata."""
    started = _utc_now()
    before = resource_snapshot(pid=pid, db_path=db_path)
    outcome = "ok"
    returncode: int | None = None
    timed_out = False
    completed: subprocess.CompletedProcess[str] | None = None
    launch_error: str | None = None
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        returncode = completed.returncode
        if returncode != 0:
            outcome = "nonzero"
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        timed_out = True
    except OSError as exc:
        outcome = "launch_error"
        returncode = 127
        launch_error = str(exc)
    ended = _utc_now()
    after = resource_snapshot(pid=pid, db_path=db_path)
    metadata: dict[str, Any] = {
        "argv": list(argv),
        "returncode": returncode,
        "timed_out": timed_out,
    }
    if launch_error is not None:
        metadata["launch_error"] = launch_error
    if completed is not None:
        metadata.update(
            {
                "stdout_bytes": len(completed.stdout.encode()),
                "stderr_bytes": len(completed.stderr.encode()),
            }
        )
    return make_record(
        workload=workload,
        adapter=adapter,
        started_at=started,
        ended_at=ended,
        outcome=outcome,
        before=before,
        after=after,
        metadata=metadata,
    )


def summarize_log(path: Path, *, keywords: Sequence[str]) -> dict[str, Any]:
    """Count named events without exporting log contents."""
    counts: Counter[str] = Counter()
    timestamped: list[str] = []
    lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines += 1
            lowered = line.lower()
            for keyword in keywords:
                if keyword.lower() in lowered:
                    counts[keyword] += 1
                    match = _TIMESTAMP_RE.match(line)
                    if match:
                        timestamped.append(match.group("timestamp"))
    return {
        "path": str(path),
        "lines": lines,
        "keyword_counts": dict(counts),
        "first_event_timestamp": min(timestamped) if timestamped else None,
        "last_event_timestamp": max(timestamped) if timestamped else None,
    }


def compare_records(left: Sequence[dict[str, Any]], right: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Compare like-for-like adapters; refuse silent cross-workload mixing."""
    left_keys = {(r.get("workload"), r.get("adapter")) for r in left}
    right_keys = {(r.get("workload"), r.get("adapter")) for r in right}
    if left_keys != right_keys:
        raise ValueError(
            "workload/adapter sets differ; use explicit adapters instead of a universal score"
        )
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "comparisons": []}
    for key in sorted(left_keys):
        l = [r for r in left if (r.get("workload"), r.get("adapter")) == key]
        r = [r for r in right if (r.get("workload"), r.get("adapter")) == key]
        result["comparisons"].append(
            {
                "workload": key[0],
                "adapter": key[1],
                "left_runs": len(l),
                "right_runs": len(r),
                "left_duration_ms": [x.get("duration_ms") for x in l],
                "right_duration_ms": [x.get("duration_ms") for x in r],
                "left_outcomes": [x.get("outcome") for x in l],
                "right_outcomes": [x.get("outcome") for x in r],
            }
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    snap = sub.add_parser("snapshot", help="capture local resources without model calls")
    snap.add_argument("--pid", type=int)
    snap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    command = sub.add_parser("command", help="run one bounded local command")
    command.add_argument("--workload", required=True)
    command.add_argument("--adapter", required=True)
    command.add_argument("--pid", type=int)
    command.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    command.add_argument("--timeout", type=float, default=60.0)
    command.add_argument("argv", nargs=argparse.REMAINDER)

    log = sub.add_parser("log-summary", help="count events in a log without exporting contents")
    log.add_argument("path", type=Path)
    log.add_argument("keywords", nargs="+", default=["compression"])

    compare = sub.add_parser("compare", help="compare two JSON arrays of probe records")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "snapshot":
        print(json.dumps(resource_snapshot(pid=args.pid, db_path=args.db), ensure_ascii=False))
        return 0
    if args.action == "command":
        argv_value = list(args.argv)
        if argv_value and argv_value[0] == "--":
            argv_value = argv_value[1:]
        if not argv_value:
            raise SystemExit("command requires an argv after --")
        print(
            json.dumps(
                run_command(
                    argv_value,
                    workload=args.workload,
                    adapter=args.adapter,
                    pid=args.pid,
                    db_path=args.db,
                    timeout=args.timeout,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if args.action == "log-summary":
        print(json.dumps(summarize_log(args.path, keywords=args.keywords), ensure_ascii=False))
        return 0
    left = json.loads(args.left.read_text(encoding="utf-8"))
    right = json.loads(args.right.read_text(encoding="utf-8"))
    print(json.dumps(compare_records(left, right), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
