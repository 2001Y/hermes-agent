"""Regression tests for cron failure severity presentation."""

from cron.scheduler import _cron_recovery_message, _summarize_cron_failure_for_delivery


def test_cron_failure_uses_alert_emoji_not_warning_emoji():
    message = _summarize_cron_failure_for_delivery(
        {"name": "LLM cron"},
        "provider quota exhausted",
    )

    assert message.startswith("🚨 ")
    assert "⚠️" not in message


def test_cron_recovery_remains_success_emoji():
    message = _cron_recovery_message({"name": "LLM cron"}, 2)

    assert message.startswith("✅ ")
    assert "recovered after 2 consecutive failure(s)" in message
