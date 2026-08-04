"""Global security.redact_secrets=False semantics across all redaction planes."""

import pytest

from agent.moa_loop import _redact_reference_text
from agent.display import (
    redact_browser_typed_text_for_display,
    redact_tool_args_for_display,
)
from agent.monitoring.redaction import redact_for_export
from agent.redact import redact_cdp_url, redact_sensitive_text
from gateway.platforms.helpers import redact_phone
from gateway.run import _redact_gateway_user_facing_secrets
from hermes_cli.debug import _redact_log_text
from plugins.platforms.a2a.security import redact_outbound
from plugins.platforms.google_chat.adapter import _redact_sensitive
from tools.send_message_tool import _sanitize_error_text
from tools.delegate_tool import _sanitize_tool_target
from tools.mcp_tool import _sanitize_error


SECRET = "sk-proj-" + ("a" * 40)
RAW_EMAIL = "person@example.com"
RAW_UUID = "12345678-1234-1234-1234-123456789abc"
RAW_PHONE = "+1 212 555 0199"


@pytest.fixture
def redaction_disabled(monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)


def test_force_redaction_is_disabled_at_common_entrypoint(redaction_disabled):
    text = f"token={SECRET} email={RAW_EMAIL}"
    assert redact_sensitive_text(text, force=True) == text


def test_cdp_url_extra_redaction_is_disabled(redaction_disabled):
    url = f"https://user:password@example.com/callback?access_token={SECRET}"
    assert redact_cdp_url(url) == url


def test_browser_type_display_redaction_is_disabled(redaction_disabled):
    payload = {"message": f"typed {SECRET}", "nested": [SECRET]}
    assert redact_browser_typed_text_for_display(payload, SECRET) == payload
    assert redact_tool_args_for_display("browser_type", {"text": SECRET}) == {
        "text": SECRET
    }


def test_monitoring_secret_and_pii_redaction_is_disabled(redaction_disabled):
    text = f"{SECRET} {RAW_EMAIL} {RAW_UUID} {RAW_PHONE}"
    assert redact_for_export(text) == text


def test_gateway_phone_redaction_is_disabled(redaction_disabled):
    phone = "+1 212 555 0199"
    assert redact_phone(phone) == phone


def test_gateway_secret_redaction_is_disabled(redaction_disabled):
    text = f"gateway response contains {SECRET}"
    assert _redact_gateway_user_facing_secrets(text) == text


def test_send_message_error_extra_redaction_is_disabled(redaction_disabled):
    text = "request failed https://example.test/?access_token=opaque-token"
    assert _sanitize_error_text(text) == text


def test_a2a_outbound_redaction_is_disabled(redaction_disabled):
    text = f"peer payload {SECRET} {RAW_EMAIL}"
    assert redact_outbound(text) == text


def test_google_chat_error_redaction_is_disabled(redaction_disabled):
    text = (
        "projects/private-project/subscriptions/private-subscription "
        "service-account@private-project.iam.gserviceaccount.com"
    )
    assert _redact_sensitive(text) == text


def test_moa_privacy_redaction_is_disabled(redaction_disabled):
    text = f"advisor {SECRET} {RAW_EMAIL} 555-123-4567"
    assert _redact_reference_text(text) == text


def test_mcp_error_redaction_is_disabled(redaction_disabled):
    text = f"MCP failure token={SECRET}"
    assert _sanitize_error(text) == text


def test_delegation_url_redaction_is_disabled(redaction_disabled):
    url = f"https://user:password@example.com/callback?token={SECRET}"
    assert _sanitize_tool_target("url", url) == url


def test_debug_upload_redaction_is_disabled(redaction_disabled):
    text = f"contact {RAW_EMAIL}; key={SECRET}"
    assert _redact_log_text(text) == text
