"""Slack Calls and Realtime control-plane primitives for Hermes Live.

Slack's Calls API registers an external call URL; it does not expose native
Huddles media to a bot.  This module deliberately keeps three concerns
separate:

* ``LiveCallRegistry`` owns short-lived, opaque browser-session URLs.
* ``SlackCallsClient`` owns the small Slack Web API surface used to advertise
  and close an external call.
* ``SlackLiveBridge`` owns the local Realtime/browser control plane and the
  callback into Hermes Core.

The browser page is intentionally served by Hermes rather than embedding an
OpenAI API key.  It obtains an ephemeral Realtime client secret from the
server, establishes WebRTC directly with OpenAI, and calls the local Hermes
control endpoints for Core work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional
from urllib.parse import quote, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


def _configured_value(
    settings: Optional[Mapping[str, Any]],
    key: str,
    env_name: str,
    default: Any = None,
) -> Any:
    """Read non-secret Live settings from platform config before environment."""
    if isinstance(settings, Mapping) and key in settings and settings[key] is not None:
        return settings[key]
    return os.getenv(env_name, default)


def _resolve_openai_realtime_credential() -> tuple[Optional[str], str]:
    """Resolve the active Hermes Codex OAuth token or a direct API key.

    Realtime accepts the same OAuth bearer used by Hermes's ``openai-codex``
    provider.  The token is kept server-side and is exchanged for a short-lived
    browser credential by ``/realtime/client_secrets``; it is never rendered in
    the join page.  OAuth is preferred so a configured API key is not silently
    used for a ChatGPT-subscription-backed Hermes session.
    """
    oauth_token = ""
    try:
        from hermes_cli.auth import resolve_codex_runtime_credentials

        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        oauth_token = str(credentials.get("api_key") or "").strip()
    except Exception as exc:  # pragma: no cover - auth-store failure path
        logger.info(
            "Hermes Live could not resolve openai-codex OAuth credentials: %s",
            type(exc).__name__,
        )
    if oauth_token:
        return oauth_token, "openai-codex-oauth"

    try:
        from agent.secret_scope import get_secret

        direct_key = str(get_secret("OPENAI_API_KEY") or "").strip()
    except Exception as exc:  # pragma: no cover - profile scope failure path
        logger.info("Hermes Live could not resolve direct Realtime credentials: %s", type(exc).__name__)
        direct_key = ""
    if direct_key:
        return direct_key, "api-key"
    return None, "unconfigured"


class SlackLiveError(RuntimeError):
    """A safe, user-facing error in the Slack Live control plane."""


class LiveCallStatus(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    ENDED = "ended"
    EXPIRED = "expired"


class LiveRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class LiveCallSession:
    """An opaque browser session associated with one Slack call."""

    session_id: str
    token: str
    team_id: str
    channel_id: str
    user_id: str
    join_url: str
    created_at: float
    expires_at: float
    title: str = "Hermes Live"
    call_id: Optional[str] = None
    status: LiveCallStatus = LiveCallStatus.CREATED

    def public_dict(self) -> dict[str, Any]:
        """Return session metadata safe for the browser UI."""
        return {
            "session_id": self.session_id,
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "call_id": self.call_id,
        }


@dataclass
class LiveRun:
    """One Hermes Core request submitted from the Live companion."""

    run_id: str
    session_id: str
    prompt: str
    created_at: float
    updated_at: float
    status: LiveRunStatus = LiveRunStatus.QUEUED
    result: Optional[str] = None
    error: Optional[str] = None
    events: List[dict[str, Any]] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "events": list(self.events),
        }


class LiveCallRegistry:
    """In-memory registry for short-lived, unguessable Live sessions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_ttl_seconds: float = 3600.0,
    ) -> None:
        self._clock = clock
        self._default_ttl_seconds = default_ttl_seconds
        self._sessions: Dict[str, LiveCallSession] = {}

    @staticmethod
    def _make_join_url(base_url: str, token: str) -> str:
        parts = urlsplit((base_url or "").strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("join_url_base must use an http or https URL")
        if parts.username or parts.password:
            raise ValueError("join_url_base must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("join_url_base must not contain a query or fragment")
        base_path = parts.path.rstrip("/")
        if not base_path.endswith("/live"):
            base_path = f"{base_path}/live"
        path = f"{base_path}/{quote(token, safe='')}"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def create(
        self,
        *,
        team_id: str,
        channel_id: str,
        user_id: str,
        join_url_base: str,
        title: str = "Hermes Live",
        ttl_seconds: Optional[float] = None,
    ) -> LiveCallSession:
        ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not channel_id or not user_id:
            raise ValueError("channel_id and user_id are required")

        token = secrets.token_urlsafe(32)
        now = self._clock()
        session = LiveCallSession(
            session_id=f"live_{uuid.uuid4().hex}",
            token=token,
            team_id=team_id or "",
            channel_id=channel_id,
            user_id=user_id,
            join_url=self._make_join_url(join_url_base, token),
            created_at=now,
            expires_at=now + ttl,
            title=(title or "Hermes Live").strip() or "Hermes Live",
        )
        self._sessions[token] = session
        return session

    def get(self, token: str) -> Optional[LiveCallSession]:
        session = self._sessions.get(token or "")
        if session is None:
            return None
        if self._clock() >= session.expires_at:
            session.status = LiveCallStatus.EXPIRED
            self._sessions.pop(token, None)
            return None
        if session.status in {LiveCallStatus.ENDED, LiveCallStatus.EXPIRED}:
            return None
        return session

    def require(self, token: str) -> LiveCallSession:
        session = self.get(token)
        if session is None:
            raise SlackLiveError("Live session not found or expired")
        return session

    def find_active(
        self,
        *,
        team_id: str,
        channel_id: str,
        user_id: str,
    ) -> Optional[LiveCallSession]:
        """Find the caller's active session without exposing other tokens."""
        for session in list(self._sessions.values()):
            if (
                session.team_id == (team_id or "")
                and session.channel_id == channel_id
                and session.user_id == user_id
            ):
                if self.get(session.token) is not None:
                    return session
        return None

    def close(self, token: str) -> None:
        session = self._sessions.pop(token or "", None)
        if session is not None:
            session.status = LiveCallStatus.ENDED


class LiveRunRegistry:
    """Async run registry that keeps the Live surface separate from Core."""

    def __init__(
        self,
        *,
        core_handler: Optional[
            Callable[[LiveCallSession, str], Awaitable[Optional[str]]]
        ] = None,
        clock: Callable[[], float] = time.time,
        max_runs_per_session: int = 32,
    ) -> None:
        self._core_handler = core_handler
        self._clock = clock
        self._max_runs_per_session = max(1, max_runs_per_session)
        self._runs: Dict[str, LiveRun] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def _event(self, run: LiveRun, event_type: str, **data: Any) -> None:
        run.updated_at = self._clock()
        run.events.append({"type": event_type, "at": run.updated_at, **data})
        if len(run.events) > 100:
            del run.events[:-100]

    async def _execute(self, run: LiveRun, session: LiveCallSession) -> None:
        run.status = LiveRunStatus.RUNNING
        self._event(run, "core_started")
        if self._core_handler is None:
            run.status = LiveRunStatus.FAILED
            run.error = "Hermes Core callback is not configured"
            self._event(run, "core_failed", error=run.error)
            return
        try:
            result = await self._core_handler(session, run.prompt)
            run.result = None if result is None else str(result)
            run.status = LiveRunStatus.COMPLETED
            self._event(run, "core_completed")
        except asyncio.CancelledError:
            run.status = LiveRunStatus.FAILED
            run.error = "Live run cancelled"
            self._event(run, "core_cancelled")
            raise
        except Exception as exc:  # pragma: no cover - exercised by focused failure test
            logger.exception("Hermes Live Core callback failed")
            run.status = LiveRunStatus.FAILED
            run.error = str(exc) or exc.__class__.__name__
            self._event(run, "core_failed", error=run.error)

    async def submit(self, session: LiveCallSession, prompt: str) -> LiveRun:
        prompt = (prompt or "").strip()
        if not prompt:
            raise SlackLiveError("prompt is required")
        if session.status in {LiveCallStatus.ENDED, LiveCallStatus.EXPIRED}:
            raise SlackLiveError("Live session is not active")

        existing = [r for r in self._runs.values() if r.session_id == session.session_id]
        if len(existing) >= self._max_runs_per_session:
            raise SlackLiveError("Live session has reached its run limit")

        now = self._clock()
        run = LiveRun(
            run_id=f"run_{uuid.uuid4().hex}",
            session_id=session.session_id,
            prompt=prompt,
            created_at=now,
            updated_at=now,
        )
        self._runs[run.run_id] = run
        self._event(run, "queued")
        task = asyncio.create_task(self._execute(run, session))
        self._tasks[run.run_id] = task
        return run

    def get(self, session: LiveCallSession, run_id: str) -> Optional[LiveRun]:
        run = self._runs.get(run_id)
        if run is None or run.session_id != session.session_id:
            return None
        return run

    async def wait(self, run_id: str, *, timeout: float = 30.0) -> LiveRun:
        task = self._tasks.get(run_id)
        if task is None:
            raise SlackLiveError("Live run not found")
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        run = self._runs.get(run_id)
        if run is None:  # defensive; tasks are retained for readback
            raise SlackLiveError("Live run disappeared")
        return run


class SlackCallsClient:
    """Small adapter around the Slack Web API Calls methods."""

    @staticmethod
    def _check(response: Any, operation: str) -> Any:
        ok = response.get("ok") if hasattr(response, "get") else None
        if ok is False:
            error = response.get("error", "unknown_error")
            raise SlackLiveError(f"Slack {operation} failed: {error}")
        return response

    async def add(
        self,
        client: Any,
        *,
        external_unique_id: str,
        join_url: str,
        title: str,
        created_by: str,
    ) -> str:
        response = self._check(
            await client.calls_add(
                external_unique_id=external_unique_id,
                join_url=join_url,
                title=title,
                created_by=created_by,
            ),
            "calls.add",
        )
        call = response.get("call") if hasattr(response, "get") else None
        call_id = call.get("id") if isinstance(call, dict) else None
        if not call_id:
            raise SlackLiveError("Slack calls.add returned no call id")
        return str(call_id)

    async def post_message(
        self,
        client: Any,
        *,
        channel_id: str,
        call_id: str,
        thread_ts: Optional[str] = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "text": "Hermes Live",
            "blocks": [{"type": "call", "call_id": call_id}],
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return self._check(
            await client.chat_postMessage(**kwargs),
            "chat.postMessage",
        )

    async def end(self, client: Any, call_id: str) -> Any:
        return self._check(await client.calls_end(id=call_id), "calls.end")


class SlackLiveBridge:
    """Own one Slack Live control plane and its Hermes Core callback."""

    def __init__(
        self,
        *,
        join_url_base: Optional[str],
        core_handler: Optional[
            Callable[[LiveCallSession, str], Awaitable[Optional[str]]]
        ] = None,
        openai_api_key: Optional[str] = None,
        openai_auth_mode: str = "unconfigured",
        realtime_model: str = "gpt-realtime-2.1",
        openai_safety_identifier: Optional[str] = None,
        session_ttl_seconds: float = 3600.0,
    ) -> None:
        self.join_url_base = (join_url_base or "").strip() or None
        self.openai_api_key = openai_api_key
        self.openai_auth_mode = openai_auth_mode
        self.realtime_model = realtime_model
        self.openai_safety_identifier = openai_safety_identifier
        self.sessions = LiveCallRegistry(default_ttl_seconds=session_ttl_seconds)
        self.runs = LiveRunRegistry(core_handler=core_handler)
        self.slack_calls = SlackCallsClient()

    @classmethod
    def from_env(
        cls,
        *,
        core_handler: Optional[
            Callable[[LiveCallSession, str], Awaitable[Optional[str]]]
        ] = None,
        settings: Optional[Mapping[str, Any]] = None,
    ) -> "SlackLiveBridge":
        ttl_raw = _configured_value(settings, "live_session_ttl", "SLACK_LIVE_SESSION_TTL", "3600")
        try:
            ttl = float(ttl_raw)
        except (TypeError, ValueError):
            ttl = 3600.0
        if ttl <= 0:
            ttl = 3600.0
        openai_api_key, openai_auth_mode = _resolve_openai_realtime_credential()

        return cls(
            join_url_base=_configured_value(
                settings,
                "live_join_url_base",
                "SLACK_LIVE_JOIN_URL_BASE",
            ),
            core_handler=core_handler,
            openai_api_key=openai_api_key,
            openai_auth_mode=openai_auth_mode,
            realtime_model=str(
                _configured_value(
                    settings,
                    "live_realtime_model",
                    "SLACK_LIVE_REALTIME_MODEL",
                    "gpt-realtime-2.1",
                )
                or "gpt-realtime-2.1"
            ).strip(),
            openai_safety_identifier=(
                str(
                    _configured_value(
                        settings,
                        "live_safety_identifier",
                        "SLACK_LIVE_SAFETY_IDENTIFIER",
                        "",
                    )
                    or ""
                ).strip()
                or None
            ),
            session_ttl_seconds=ttl,
        )

    @property
    def configured(self) -> bool:
        return bool(self.join_url_base)

    async def open_call(
        self,
        client: Any,
        *,
        team_id: str,
        channel_id: str,
        user_id: str,
        title: str = "Hermes Live",
        thread_ts: Optional[str] = None,
    ) -> LiveCallSession:
        if not self.join_url_base:
            raise SlackLiveError(
                "SLACK_LIVE_JOIN_URL_BASE is not configured; no Slack call was created"
            )

        session = self.sessions.create(
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            join_url_base=self.join_url_base,
            title=title,
        )
        call_id: Optional[str] = None
        try:
            call_id = await self.slack_calls.add(
                client,
                external_unique_id=session.session_id,
                join_url=session.join_url,
                title=session.title,
                created_by=user_id,
            )
            session.call_id = call_id
            session.status = LiveCallStatus.ACTIVE
            await self.slack_calls.post_message(
                client,
                channel_id=channel_id,
                call_id=call_id,
                thread_ts=thread_ts,
            )
            return session
        except Exception:
            if call_id:
                try:
                    await self.slack_calls.end(client, call_id)
                except Exception:
                    logger.warning("Failed to clean up Slack Live call %s", call_id, exc_info=True)
            self.sessions.close(session.token)
            raise

    async def end_call(self, client: Any, token: str) -> None:
        session = self.sessions.require(token)
        if session.call_id:
            await self.slack_calls.end(client, session.call_id)
        self.sessions.close(token)

    def realtime_session_payload(self, session: LiveCallSession) -> dict[str, Any]:
        """Return the GA Realtime client-secret request body."""
        return {
            "session": {
                "type": "realtime",
                "model": self.realtime_model,
                "audio": {"output": {"voice": "marin"}},
            }
        }

    def safety_identifier(self, session: LiveCallSession) -> str:
        if self.openai_safety_identifier:
            return self.openai_safety_identifier
        return hashlib.sha256(
            f"hermes-slack-live:{session.team_id}:{session.user_id}".encode()
        ).hexdigest()


class LiveServer:
    """Lifecycle wrapper for the optional in-process ASGI Live server."""

    def __init__(self, bridge: SlackLiveBridge, *, host: str, port: int) -> None:
        self.bridge = bridge
        self.host = host
        self.port = port
        self.app: Any = None
        self._server: Any = None
        self._task: Optional[asyncio.Task] = None

    @classmethod
    def from_env(
        cls,
        bridge: SlackLiveBridge,
        settings: Optional[Mapping[str, Any]] = None,
    ) -> "LiveServer":
        raw_port = _configured_value(settings, "live_port", "SLACK_LIVE_PORT", "8787")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 8787
        if not 1 <= port <= 65535:
            port = 8787
        host = str(
            _configured_value(settings, "live_host", "SLACK_LIVE_HOST", "127.0.0.1")
            or "127.0.0.1"
        ).strip()
        return cls(
            bridge,
            host=host,
            port=port,
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover - runtime dependency path
            raise SlackLiveError("uvicorn is required to serve Hermes Live") from exc

        self.app = create_live_app(self.bridge)
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

        deadline = time.monotonic() + 5.0
        while not self._server.started:
            if self._task.done():
                await self._task
                raise SlackLiveError("Hermes Live server stopped during startup")
            if time.monotonic() >= deadline:
                raise SlackLiveError("Hermes Live server did not become ready")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        server = self._server
        task = self._task
        self._server = None
        self._task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass


def _render_live_page(token: str, model: str) -> str:
    token_json = json.dumps(token)
    model_json = json.dumps(model)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes Live</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
    body {{ max-width: 48rem; margin: 3rem auto; padding: 0 1rem; background: #111827; color: #f9fafb; }}
    button {{ border: 0; border-radius: .5rem; padding: .75rem 1rem; background: #f97316; color: white; font-weight: 700; cursor: pointer; }}
    button[disabled] {{ opacity: .5; cursor: not-allowed; }}
    #status {{ margin: 1rem 0; color: #fdba74; }}
    #log {{ white-space: pre-wrap; min-height: 8rem; padding: 1rem; border: 1px solid #374151; border-radius: .5rem; background: #030712; }}
  </style>
</head>
<body>
  <h1>Hermes Live</h1>
  <p>音声でHermesに依頼できます。実行・ファイル変更・外部書込みはHermes Coreの通常の権限境界に従います。</p>
  <button id="start">Start live conversation</button>
  <div id="status">Disconnected</div>
  <div id="log"></div>
  <script>
  const TOKEN = {token_json};
  const MODEL = {model_json};
  const startButton = document.getElementById("start");
  const status = document.getElementById("status");
  const log = document.getElementById("log");
  let dataChannel;
  let peerConnection;

  function write(message) {{
    log.textContent += `${{message}}\\n`;
    log.scrollTop = log.scrollHeight;
  }}

  async function hermesTool(name, args) {{
    if (name === "hermes_submit") {{
      const response = await fetch(`/live/${{TOKEN}}/runs`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ prompt: args.prompt || "" }}),
      }});
      return await response.json();
    }}
    if (name === "hermes_status") {{
      const response = await fetch(`/live/${{TOKEN}}/runs/${{encodeURIComponent(args.run_id || "")}}`);
      return await response.json();
    }}
    return {{ error: `Unknown Hermes tool: ${{name}}` }};
  }}

  function sendEvent(event) {{
    if (!dataChannel || dataChannel.readyState !== "open") throw new Error("Realtime data channel is not open");
    dataChannel.send(JSON.stringify(event));
  }}

  async function handleServerEvent(event) {{
    if (event.type === "response.function_call_arguments.done") {{
      const args = JSON.parse(event.arguments || "{{}}\");
      const output = await hermesTool(event.name, args);
      sendEvent({{
        type: "conversation.item.create",
        item: {{ type: "function_call_output", call_id: event.call_id, output: JSON.stringify(output) }}
      }});
      sendEvent({{ type: "response.create" }});
      return;
    }}
    if (event.type === "response.output_text.delta") {{
      write(event.delta || "");
    }} else if (event.type === "error") {{
      write(`Realtime error: ${{event.error?.message || "unknown error"}}`);
    }} else if (event.type === "session.created") {{
      status.textContent = "Connected — speak naturally";
    }}
  }}

  async function startLive() {{
    startButton.disabled = true;
    status.textContent = "Connecting…";
    try {{
      const tokenResponse = await fetch(`/live/${{TOKEN}}/realtime-token`, {{ method: "POST" }});
      const tokenData = await tokenResponse.json();
      if (!tokenResponse.ok) throw new Error(tokenData.detail || "Token request failed");
      const ephemeral = tokenData.value;
      peerConnection = new RTCPeerConnection();
      const audio = document.createElement("audio");
      audio.autoplay = true;
      peerConnection.ontrack = (event) => {{ audio.srcObject = event.streams[0]; }};
      const microphone = await navigator.mediaDevices.getUserMedia({{ audio: true }});
      peerConnection.addTrack(microphone.getTracks()[0]);
      dataChannel = peerConnection.createDataChannel("oai-events");
      dataChannel.addEventListener("message", (message) => {{
        handleServerEvent(JSON.parse(message.data)).catch((error) => write(`Tool error: ${{error.message}}`));
      }});
      dataChannel.addEventListener("open", () => {{
        sendEvent({{
          type: "session.update",
          session: {{
            type: "realtime",
            model: MODEL,
            instructions: "You are the Hermes Live voice companion. Use hermes_submit for real Hermes work, and hermes_status to report its progress. Do not claim work is complete unless the tool reports it.",
            tools: [
              {{ type: "function", name: "hermes_submit", description: "Submit a user request to Hermes Core.", parameters: {{ type: "object", properties: {{ prompt: {{ type: "string" }} }}, required: ["prompt"] }} }},
              {{ type: "function", name: "hermes_status", description: "Read the status of a Hermes Core run.", parameters: {{ type: "object", properties: {{ run_id: {{ type: "string" }} }}, required: ["run_id"] }} }}
            ]
          }}
        }});
      }});
      const offer = await peerConnection.createOffer();
      await peerConnection.setLocalDescription(offer);
      const answerResponse = await fetch("https://api.openai.com/v1/realtime/calls", {{
        method: "POST",
        body: offer.sdp,
        headers: {{ Authorization: `Bearer ${{ephemeral}}`, "Content-Type": "application/sdp" }}
      }});
      if (!answerResponse.ok) throw new Error(`WebRTC handshake failed (${{answerResponse.status}})`);
      await peerConnection.setRemoteDescription({{ type: "answer", sdp: await answerResponse.text() }});
      write("Live session connected.");
    }} catch (error) {{
      startButton.disabled = false;
      status.textContent = "Connection failed";
      write(error.message || String(error));
    }}
  }}
  startButton.addEventListener("click", startLive);
  </script>
</body>
</html>"""


def create_live_app(bridge: SlackLiveBridge) -> Any:
    """Create the ASGI app used as the Slack Call ``join_url`` target."""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise SlackLiveError("FastAPI is required to serve Hermes Live") from exc

    app = FastAPI(title="Hermes Live", docs_url=None, redoc_url=None)

    def session_or_404(token: str) -> LiveCallSession:
        session = bridge.sessions.get(token)
        if session is None:
            raise HTTPException(status_code=404, detail="Live session not found or expired")
        return session

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/live/{token}", response_class=HTMLResponse)
    async def live_page(token: str) -> str:
        session_or_404(token)
        return _render_live_page(token, bridge.realtime_model)

    @app.get("/live/{token}/session")
    async def live_session(token: str) -> dict[str, Any]:
        return session_or_404(token).public_dict()

    @app.post("/live/{token}/realtime-token")
    async def realtime_token(token: str) -> Any:
        session = session_or_404(token)
        if not bridge.openai_api_key:
            raise HTTPException(
                status_code=503,
                detail="Realtime server credentials are not configured",
            )
        try:
            import httpx

            headers = {
                "Authorization": f"Bearer {bridge.openai_api_key}",
                "Content-Type": "application/json",
                "OpenAI-Safety-Identifier": bridge.safety_identifier(session),
            }
            async with httpx.AsyncClient(timeout=20.0) as http:
                response = await http.post(
                    "https://api.openai.com/v1/realtime/client_secrets",
                    headers=headers,
                    json=bridge.realtime_session_payload(session),
                )
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("Hermes Live ephemeral token request failed: %s", exc)
            raise HTTPException(status_code=502, detail="Realtime token request failed") from exc
        if response.is_error:
            logger.warning(
                "Hermes Live ephemeral token request returned HTTP %s",
                response.status_code,
            )
            raise HTTPException(status_code=502, detail="Realtime token request failed")
        return JSONResponse(response.json())

    @app.post("/live/{token}/runs", status_code=202)
    async def submit_run(token: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = session_or_404(token)
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        if len(prompt) > 20_000:
            raise HTTPException(status_code=413, detail="prompt is too long")
        try:
            run = await bridge.runs.submit(session, prompt)
        except SlackLiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": run.run_id, "status": run.status.value}

    @app.get("/live/{token}/runs/{run_id}")
    async def run_status(token: str, run_id: str) -> Any:
        session = session_or_404(token)
        run = bridge.runs.get(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Live run not found")
        return run.public_dict()

    return app


__all__ = [
    "LiveCallRegistry",
    "LiveCallSession",
    "LiveCallStatus",
    "LiveRun",
    "LiveRunRegistry",
    "LiveRunStatus",
    "SlackCallsClient",
    "SlackLiveBridge",
    "SlackLiveError",
    "LiveServer",
    "create_live_app",
]
