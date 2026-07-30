"""Focused tests for durable cron failure notification suppression."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import stat

from cron.failure_notifications import (
    CronFailureNotifier,
    normalize_failure_text,
)


UTC = timezone.utc


def _now(second: int = 0) -> datetime:
    return datetime(2026, 7, 30, 9, 0, second, tzinfo=UTC)


def test_same_failure_notifies_once_and_normalizes_run_noise(tmp_path):
    notifier = CronFailureNotifier(tmp_path / "state.json")
    first = notifier.record_failure(
        "hermes-cron:j1",
        "Timeout at 2026-07-30T09:00:00Z request_id=abc123 attempt=2",
        now=_now(),
    )
    second = notifier.record_failure(
        "hermes-cron:j1",
        "Timeout at 2026-07-30T09:01:00Z request_id=def456 attempt=3",
        now=_now(1),
    )

    assert first.notify is True
    assert second.notify is False
    assert second.reason == "duplicate"
    assert second.consecutive_count == 2
    assert normalize_failure_text(first.normalized_error) == first.normalized_error


def test_materially_different_error_fingerprint_notifies_again(tmp_path):
    notifier = CronFailureNotifier(tmp_path / "state.json")
    first = notifier.record_failure("hermes-cron:j1", "HTTP 429 quota", now=_now())
    changed = notifier.record_failure("hermes-cron:j1", "HTTP 401 unauthorized", now=_now(1))

    assert first.notify is True
    assert changed.notify is True
    assert changed.reason == "changed"
    assert changed.fingerprint != first.fingerprint
    assert changed.consecutive_count == 1


def test_recovery_notifies_once_and_returns_to_healthy(tmp_path):
    notifier = CronFailureNotifier(tmp_path / "state.json")
    notifier.record_failure("hermes-cron:j1", "script failed", now=_now())

    recovered = notifier.record_success("hermes-cron:j1", now=_now(1))
    repeated_healthy = notifier.record_success("hermes-cron:j1", now=_now(2))

    assert recovered.notify is True
    assert recovered.consecutive_count == 1
    assert repeated_healthy.notify is False
    assert repeated_healthy.reason == "already_healthy"


def test_restart_continuity_uses_durable_state(tmp_path):
    state_path = tmp_path / "state.json"
    first_process = CronFailureNotifier(state_path)
    assert first_process.record_failure("hermes-cron:j1", "boom", now=_now()).notify

    second_process = CronFailureNotifier(state_path)
    repeated = second_process.record_failure("hermes-cron:j1", "boom", now=_now(1))

    assert repeated.notify is False
    assert repeated.reason == "duplicate"
    payload = json.loads(state_path.read_text())
    assert payload["version"] == 1
    assert payload["incidents"]["hermes-cron:j1"]["consecutive_count"] == 2
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_concurrent_writers_claim_only_one_first_notification(tmp_path):
    notifier = CronFailureNotifier(tmp_path / "state.json")

    def record(_index: int):
        return notifier.record_failure("hermes-cron:j1", "same failure", now=_now())

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(record, range(8)))

    assert sum(decision.notify for decision in decisions) == 1
    assert max(decision.consecutive_count for decision in decisions) == 8


def test_slack_transport_rejection_opens_only_slack_circuit(tmp_path):
    notifier = CronFailureNotifier(
        tmp_path / "state.json",
        max_attempts_per_window=3,
        window_seconds=900,
        circuit_cooldown_seconds=1800,
    )
    assert notifier.claim_failure_targets(["slack", "matrix"], now=_now()) == ["slack", "matrix"]

    notifier.record_transport_failure(
        "slack",
        "Slack API error: message_limit_exceeded",
        now=_now(1),
    )

    assert notifier.claim_failure_targets(["slack", "matrix"], now=_now(2)) == ["matrix"]


def test_failure_target_budget_opens_circuit_after_three_attempts(tmp_path):
    notifier = CronFailureNotifier(
        tmp_path / "state.json",
        max_attempts_per_window=3,
        window_seconds=900,
        circuit_cooldown_seconds=1800,
    )
    for second in range(3):
        assert notifier.claim_failure_targets(["slack"], now=_now(second)) == ["slack"]

    assert notifier.claim_failure_targets(["slack"], now=_now(3)) == []
    assert notifier.claim_failure_targets(["matrix"], now=_now(3)) == ["matrix"]


def test_failure_without_delivery_target_does_not_claim_notification(tmp_path):
    notifier = CronFailureNotifier(tmp_path / "state.json")
    first = notifier.record_failure(
        "hermes-cron:j1",
        "local-only failure",
        now=_now(),
        notification_possible=False,
    )
    later_with_target = notifier.record_failure(
        "hermes-cron:j1",
        "local-only failure",
        now=_now(1),
        notification_possible=True,
    )

    assert first.notify is False
    assert later_with_target.notify is True
    assert later_with_target.consecutive_count == 2


def _patch_run_one_job_pipeline(monkeypatch, scheduler, responses, tmp_path):
    deliveries = []
    marks = []
    response_iter = iter(responses)

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "create_execution", lambda *args, **kwargs: {"id": "exec-1"})
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _execution_id: None)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler, "save_job_output", lambda _job_id, _output: str(tmp_path / "out.md"))
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *args, **kwargs: marks.append((args, kwargs)))
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "slack", "chat_id": "C123"}],
    )

    def fake_run_job(_job, *, defer_agent_teardown=None):
        return next(response_iter)

    def fake_deliver(_job, content, *args, **kwargs):
        deliveries.append({"content": content, "kwargs": kwargs})
        return None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "_deliver_result", fake_deliver)
    return deliveries, marks


def test_run_one_job_deduplicates_repeated_failure_delivery(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    deliveries, marks = _patch_run_one_job_pipeline(
        monkeypatch,
        scheduler,
        [
            (False, "raw-1", "", "same failure"),
            (False, "raw-2", "", "same failure"),
        ],
        tmp_path,
    )
    job = {"id": "dedup-job", "name": "Dedup job", "deliver": "slack:C123"}

    assert scheduler.run_one_job(job) is True
    assert scheduler.run_one_job(job) is True

    assert len(deliveries) == 1
    assert len(marks) == 2
    assert all(args[1] is False for args, _kwargs in marks)


def test_run_one_job_delivers_one_recovery_notice_after_failure(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    deliveries, _marks = _patch_run_one_job_pipeline(
        monkeypatch,
        scheduler,
        [
            (False, "raw-1", "", "same failure"),
            (True, "raw-2", "[SILENT]", None),
        ],
        tmp_path,
    )
    job = {"id": "recovery-job", "name": "Recovery job", "deliver": "slack:C123"}

    assert scheduler.run_one_job(job) is True
    assert scheduler.run_one_job(job) is True

    assert len(deliveries) == 2
    assert "failed" in deliveries[0]["content"]
    assert "recovered" in deliveries[1]["content"]


def test_run_one_job_skips_open_slack_circuit_but_preserves_matrix(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    from cron.failure_notifications import CronFailureNotifier

    state_path = tmp_path / "state" / "cron_failure_notifications.json"
    notifier = CronFailureNotifier(state_path)
    notifier.record_transport_failure(
        "slack",
        "message_limit_exceeded",
        now=datetime.now(timezone.utc),
    )

    deliveries, _marks = _patch_run_one_job_pipeline(
        monkeypatch,
        scheduler,
        [(False, "raw", "", "new failure")],
        tmp_path,
    )
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [
            {"platform": "slack", "chat_id": "C123"},
            {"platform": "matrix", "chat_id": "!room:example"},
        ],
    )
    job = {"id": "circuit-job", "name": "Circuit job", "deliver": "all"}

    assert scheduler.run_one_job(job) is True

    assert len(deliveries) == 1
    assert deliveries[0]["kwargs"]["failure_allowed_platforms"] == ["matrix"]


def test_delivery_limit_error_opens_slack_circuit_for_next_job(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    deliveries = []
    response_iter = iter(
        [
            (False, "raw-1", "", "failure one"),
            (False, "raw-2", "", "failure two"),
        ]
    )
    delivery_results = iter(
        [
            "delivery to slack:C123 failed: message_limit_exceeded",
            None,
        ]
    )

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "create_execution", lambda *args, **kwargs: {"id": "exec-1"})
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _execution_id: None)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler, "save_job_output", lambda _job_id, _output: str(tmp_path / "out.md"))
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "slack", "chat_id": "C123"}],
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda _job, *, defer_agent_teardown=None: next(response_iter),
    )

    def fake_deliver(_job, content, *args, **kwargs):
        deliveries.append({"content": content, "kwargs": kwargs})
        return next(delivery_results)

    monkeypatch.setattr(scheduler, "_deliver_result", fake_deliver)

    assert scheduler.run_one_job({"id": "job-a", "deliver": "slack:C123"}) is True
    assert scheduler.run_one_job({"id": "job-b", "deliver": "slack:C123"}) is True

    assert len(deliveries) == 1
    assert deliveries[0]["kwargs"]["failure_notification"] is True
