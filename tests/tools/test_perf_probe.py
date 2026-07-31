import json
from pathlib import Path

import pytest

from hermes_cli.perf_probe import (
    compare_records,
    make_record,
    parse_lsof_output,
    summarize_log,
)


def test_parse_lsof_counts_only_numeric_file_descriptors():
    text = """COMMAND PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
python  123 me     cwd  DIR  1,1       96    2 /tmp
python  123 me       0r  REG  1,1      100    3 /tmp/in
python  123 me       7u IPv4  1,2        0    4 TCP 127.0.0.1:1
python  123 me     mem  REG  1,1      100    5 /tmp/lib
"""

    assert parse_lsof_output(text) == {
        "lsof_rows": 4,
        "open_fd_count": 2,
        "highest_fd": 7,
        "fd_types": {"IPv4": 1, "REG": 1},
    }


def test_log_summary_does_not_return_log_contents(tmp_path: Path):
    path = tmp_path / "agent.log"
    path.write_text(
        "2026-08-01 00:00:00 context compression started secret-value\n"
        "2026-08-01 00:00:01 context compression finished\n",
        encoding="utf-8",
    )

    result = summarize_log(path, keywords=["compression"])

    assert result["lines"] == 2
    assert result["keyword_counts"] == {"compression": 2}
    assert "secret-value" not in json.dumps(result)


def test_compare_refuses_cross_workload_mixing():
    left = [
        make_record(
            workload="cli",
            adapter="version",
            started_at="2026-08-01T00:00:00+00:00",
            ended_at="2026-08-01T00:00:01+00:00",
            outcome="ok",
        )
    ]
    right = [
        make_record(
            workload="compression",
            adapter="version",
            started_at="2026-08-01T00:00:00+00:00",
            ended_at="2026-08-01T00:00:01+00:00",
            outcome="ok",
        )
    ]

    with pytest.raises(ValueError, match="workload/adapter"):
        compare_records(left, right)
