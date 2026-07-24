"""Contract tests for the Slack Calls -> Hermes Live control plane."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from plugins.platforms.slack.live import (
    LiveCallRegistry,
    LiveCallStatus,
    LiveRunStatus,
    LiveServer,
    SlackCallsClient,
    SlackLiveBridge,
    SlackLiveError,
    create_live_app,
)
from plugins.platforms.slack.adapter import SlackAdapter
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType


class _FakeSlackClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def calls_add(self, **kwargs):
        self.calls.append(("calls.add", kwargs))
        return {"ok": True, "call": {"id": "R-live-1"}}

    async def chat_postMessage(self, **kwargs):
        self.calls.append(("chat.postMessage", kwargs))
        return {"ok": True, "ts": "1710000000.000001"}

    async def calls_end(self, **kwargs):
        self.calls.append(("calls.end", kwargs))
        return {"ok": True}


class _FailingSlackClient:
    async def calls_add(self, **kwargs):
        return {"ok": False, "error": "missing_scope"}


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://voice.example.test/hermes", "https://voice.example.test/hermes/live/"),
        ("http://127.0.0.1:8787", "http://127.0.0.1:8787/live/"),
    ],
)
def test_session_join_url_is_opaque_and_under_configured_base(base_url, expected):
    registry = LiveCallRegistry(clock=lambda: 100.0)

    session = registry.create(
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        join_url_base=base_url,
        ttl_seconds=60,
    )

    assert session.status is LiveCallStatus.CREATED
    assert session.token not in base_url
    assert session.join_url.startswith(expected)
    assert session.join_url.endswith(session.token)
    assert registry.get(session.token) is session


def test_join_url_rejects_non_http_base():
    registry = LiveCallRegistry()

    with pytest.raises(ValueError, match="http or https"):
        registry.create(
            team_id="T1",
            channel_id="C1",
            user_id="U1",
            join_url_base="file:///tmp/hermes-live",
        )


def test_expired_session_is_not_usable():
    now = [100.0]
    registry = LiveCallRegistry(clock=lambda: now[0])
    session = registry.create(
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        join_url_base="https://voice.example.test",
        ttl_seconds=10,
    )

    now[0] = 110.001

    assert registry.get(session.token) is None


@pytest.mark.asyncio
async def test_slack_calls_client_registers_posts_and_ends_call():
    client = _FakeSlackClient()
    calls = SlackCallsClient()

    call_id = await calls.add(
        client,
        external_unique_id="live_123",
        join_url="https://voice.example.test/live/opaque",
        title="Hermes Live",
        created_by="U1",
    )
    await calls.post_message(client, channel_id="C1", call_id=call_id, thread_ts="1710.1")
    await calls.end(client, call_id)

    assert call_id == "R-live-1"
    assert client.calls[0] == (
        "calls.add",
        {
            "external_unique_id": "live_123",
            "join_url": "https://voice.example.test/live/opaque",
            "title": "Hermes Live",
            "created_by": "U1",
        },
    )
    assert client.calls[1][0] == "chat.postMessage"
    assert client.calls[1][1]["channel"] == "C1"
    assert client.calls[1][1]["thread_ts"] == "1710.1"
    assert client.calls[1][1]["blocks"] == [{"type": "call", "call_id": "R-live-1"}]
    assert client.calls[2] == ("calls.end", {"id": "R-live-1"})


@pytest.mark.asyncio
async def test_slack_calls_client_fails_closed_on_api_error():
    with pytest.raises(SlackLiveError, match="missing_scope"):
        await SlackCallsClient().add(
            _FailingSlackClient(),
            external_unique_id="live_123",
            join_url="https://voice.example.test/live/opaque",
            title="Hermes Live",
            created_by="U1",
        )


@pytest.mark.asyncio
async def test_live_control_plane_runs_core_callback_and_exposes_status():
    seen: list[tuple[str, str]] = []

    async def core_handler(session, prompt):
        seen.append((session.session_id, prompt))
        await asyncio.sleep(0)
        return f"core-result:{prompt}"

    bridge = SlackLiveBridge(
        join_url_base="https://voice.example.test",
        core_handler=core_handler,
    )
    session = bridge.sessions.create(
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        join_url_base="https://voice.example.test",
    )

    run = await bridge.runs.submit(session, "inspect the build")
    completed = await bridge.runs.wait(run.run_id, timeout=1)

    assert completed.status is LiveRunStatus.COMPLETED
    assert completed.result == "core-result:inspect the build"
    assert seen == [(session.session_id, "inspect the build")]


@pytest.mark.asyncio
async def test_live_http_app_requires_valid_session_and_submits_core_work():
    async def core_handler(session, prompt):
        return f"done:{prompt}"

    bridge = SlackLiveBridge(
        join_url_base="https://voice.example.test",
        core_handler=core_handler,
    )
    session = bridge.sessions.create(
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        join_url_base="https://voice.example.test",
    )
    app = create_live_app(bridge)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as http:
        assert (await http.get("/live/not-a-token/session")).status_code == 404
        page = await http.get(f"/live/{session.token}")
        assert page.status_code == 200
        assert session.token in page.text
        assert "RTCPeerConnection" in page.text

        response = await http.post(
            f"/live/{session.token}/runs",
            json={"prompt": "say hello"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        completed = await bridge.runs.wait(run_id, timeout=1)

        status = await http.get(f"/live/{session.token}/runs/{run_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "completed"
        assert status.json()["result"] == "done:say hello"
        assert completed.result == "done:say hello"


@pytest.mark.asyncio
async def test_realtime_token_endpoint_does_not_fallback_to_browser_api_key():
    bridge = SlackLiveBridge(
        join_url_base="https://voice.example.test",
        openai_api_key=None,
    )
    session = bridge.sessions.create(
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        join_url_base="https://voice.example.test",
    )
    app = create_live_app(bridge)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as http:
        response = await http.post(f"/live/{session.token}/realtime-token")

    assert response.status_code == 503
    assert "server" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_live_server_real_http_smoke_serves_join_page():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    bridge = SlackLiveBridge(join_url_base=base_url)
    session = bridge.sessions.create(
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        join_url_base=base_url,
    )
    server = LiveServer(bridge, host="127.0.0.1", port=port)
    await server.start()
    try:
        async with httpx.AsyncClient() as http:
            health = await http.get(f"{base_url}/healthz")
            page = await http.get(session.join_url)
        assert health.status_code == 200
        assert health.json() == {"ok": True}
        assert page.status_code == 200
        assert "RTCPeerConnection" in page.text
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_slack_native_live_command_registers_call_and_keeps_join_url_ephemeral():
    adapter = SlackAdapter(PlatformConfig(enabled=True, extra={}))
    fake_slack = _FakeSlackClient()
    bridge = SlackLiveBridge(
        join_url_base="https://voice.example.test",
        openai_api_key="server-test-key",
    )
    adapter._live_bridge = bridge
    adapter._get_client = lambda chat_id, team_id=None: fake_slack

    source = adapter.build_source(
        chat_id="C1",
        chat_type="group",
        user_id="U1",
        scope_id="T1",
    )
    event = MessageEvent(
        text="/live",
        message_type=MessageType.COMMAND,
        source=source,
        raw_message={
            "command": "/live",
            "team_id": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "response_url": "https://hooks.slack.test/ephemeral",
        },
    )

    reply = await adapter.handle_live_command(event)

    assert "Join URL:" in reply
    assert fake_slack.calls[0][0] == "calls.add"
    assert fake_slack.calls[1][0] == "chat.postMessage"
    assert "https://voice.example.test" in reply
    assert "https://voice.example.test" not in str(fake_slack.calls[1][1])


@pytest.mark.asyncio
async def test_slack_message_style_live_command_refuses_to_leak_join_url():
    adapter = SlackAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._live_bridge = SlackLiveBridge(join_url_base="https://voice.example.test")

    source = adapter.build_source(
        chat_id="C1",
        chat_type="group",
        user_id="U1",
        scope_id="T1",
    )
    event = MessageEvent(
        text="!live",
        message_type=MessageType.COMMAND,
        source=source,
        raw_message={"type": "message", "team_id": "T1"},
    )

    reply = await adapter.handle_live_command(event)

    assert "ネイティブ `/live`" in reply


@pytest.mark.asyncio
async def test_slack_live_command_does_not_create_call_without_realtime_credential():
    adapter = SlackAdapter(PlatformConfig(enabled=True, extra={}))
    fake_slack = _FakeSlackClient()
    adapter._live_bridge = SlackLiveBridge(join_url_base="https://voice.example.test")
    adapter._get_client = lambda chat_id, team_id=None: fake_slack

    source = adapter.build_source(
        chat_id="C1",
        chat_type="group",
        user_id="U1",
        scope_id="T1",
    )
    event = MessageEvent(
        text="/live",
        message_type=MessageType.COMMAND,
        source=source,
        raw_message={
            "command": "/live",
            "team_id": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "response_url": "https://hooks.slack.test/ephemeral",
        },
    )

    reply = await adapter.handle_live_command(event)

    assert "OPENAI_API_KEY" in reply
    assert fake_slack.calls == []
