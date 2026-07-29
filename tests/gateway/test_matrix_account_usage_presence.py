from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.account_usage_presence import (
    AccountUsagePresenceApplyResult,
    AccountUsagePresencePayload,
    AccountUsagePresenceRestoreResult,
)
from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter, PresenceState


@pytest.fixture
def adapter():
    config = PlatformConfig(
        enabled=True,
        token="syt_test",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:example.org",
        },
    )
    result = MatrixAdapter(config)
    result._client = MagicMock()
    result._client.mxid = "@bot:example.org"
    result._client.get_presence = AsyncMock(
        return_value=SimpleNamespace(presence="online", status_msg="Idle")
    )
    result._client.set_presence = AsyncMock()
    return result


def test_matrix_exposes_account_usage_activity_and_identity_key(adapter):
    assert adapter.account_usage_presence_capabilities.activity is True
    assert adapter.account_usage_presence_capabilities.display_name is False
    assert adapter.account_usage_presence_state_key() == "matrix:@bot:example.org"


@pytest.mark.asyncio
async def test_matrix_captures_baseline_and_renders_status(adapter):
    baseline = await adapter.capture_account_usage_presence_baseline()
    assert baseline == {"presence": "online", "status_msg": "Idle"}

    payload = AccountUsagePresencePayload(label="Session", remaining_percent=75)
    owned = adapter.build_account_usage_presence_owned_state(payload, baseline)
    assert owned == {
        "presence": "online",
        "status_msg": "Session: 75% remaining",
    }

    cached = adapter.build_account_usage_presence_owned_state(
        AccountUsagePresencePayload(
            label="Session", remaining_percent=75, cached=True
        ),
        baseline,
    )
    assert cached is not None
    assert cached["status_msg"].endswith("(cached)")


@pytest.mark.asyncio
async def test_matrix_guarded_apply_preserves_external_status(adapter):
    baseline = {"presence": "online", "status_msg": "Idle"}
    payload = AccountUsagePresencePayload(label="Session", remaining_percent=75)
    owned = adapter.build_account_usage_presence_owned_state(payload, baseline)
    assert owned is not None

    result = await adapter.apply_account_usage_presence_if_owned(
        payload, baseline, owned
    )
    assert result is AccountUsagePresenceApplyResult.EXTERNAL
    adapter._client.set_presence.assert_not_awaited()

    adapter._client.get_presence = AsyncMock(
        return_value=SimpleNamespace(
            presence="online", status_msg="Session: 75% remaining"
        )
    )
    result = await adapter.apply_account_usage_presence_if_owned(
        AccountUsagePresencePayload(label="Session", remaining_percent=70),
        baseline,
        owned,
    )
    assert result is AccountUsagePresenceApplyResult.APPLIED
    adapter._client.set_presence.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_restore_uses_compare_and_swap(adapter):
    baseline = {"presence": "online", "status_msg": "Idle"}
    owned = {"presence": "online", "status_msg": "Session: 75% remaining"}
    adapter._client.get_presence = AsyncMock(
        return_value=SimpleNamespace(
            presence="online", status_msg="Session: 75% remaining"
        )
    )

    result = await adapter.restore_account_usage_presence(baseline, owned)
    assert result is AccountUsagePresenceRestoreResult.RESTORED
    adapter._client.set_presence.assert_awaited_once_with(
        presence=PresenceState.ONLINE,
        status="Idle",
    )

    adapter._client.set_presence.reset_mock()
    adapter._client.get_presence = AsyncMock(
        return_value=SimpleNamespace(presence="online", status_msg="Manual status")
    )
    result = await adapter.restore_account_usage_presence(baseline, owned)
    assert result is AccountUsagePresenceRestoreResult.EXTERNAL
    adapter._client.set_presence.assert_not_awaited()
