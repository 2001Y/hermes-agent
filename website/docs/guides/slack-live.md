# Hermes Live over Slack Calls

Hermes Live uses Slack's Calls API as the Slack-facing call card and an external
browser/WebRTC page as the media transport. Slack's Calls API does not expose
native Huddle audio to a bot.

## Local opt-in configuration

The non-secret Live settings belong under the Slack platform's `extra` block:

```yaml
platforms:
  slack:
    extra:
      live_enabled: true
      live_join_url_base: http://127.0.0.1:8787
      live_host: 127.0.0.1
      live_port: 8787
      live_realtime_model: gpt-realtime-2.1
      live_session_ttl: 3600
```

The existing environment variables remain supported as a fallback for these
non-secret values:

```text
SLACK_LIVE_ENABLED=true
SLACK_LIVE_JOIN_URL_BASE=http://127.0.0.1:8787
SLACK_LIVE_HOST=127.0.0.1
SLACK_LIVE_PORT=8787
```

For Realtime authentication, Hermes first uses the existing `openai-codex`
Hermes OAuth credential and exchanges it for a short-lived Realtime client
secret. If that OAuth credential is unavailable, the server-side
`OPENAI_API_KEY` is used as a fallback. The OAuth token and API key never enter
the browser page or a Slack message. The OAuth-to-Realtime path was verified
with `gpt-realtime-2.1` using a session handshake, session update, text response,
and client-secret issuance; it is still an API behavior rather than a public
ChatGPT UI guarantee.

For a remote Slack client, `live_join_url_base` must be an HTTPS URL reachable
by that client. Browser microphone access requires a secure context, except for
localhost.

Generate the Slack manifest again and reinstall the Slack app after adding the
Calls scope:

```bash
hermes slack manifest --write
```

The manifest includes `calls:write` and the native `/live` command. The command
is deliberately opt-in at runtime and does not create a call until a user runs
`/live`.

## Use

- `/live` — create a Slack Call and return the bearer join URL ephemerally
- `/live status` — inspect the caller's active call
- `/live end` — end the caller's active call

Because Slack caps an app at 50 native slash commands, `/version` remains
available as `/hermes version` rather than taking the slot reserved for `/live`.

Do not use `!live` in a shared channel to start a call: the message path cannot
send the join URL ephemerally, so Hermes refuses it rather than leaking the
bearer URL into the channel.

## Runtime boundary

The browser obtains a short-lived Realtime client secret from Hermes, connects
to OpenAI Realtime over WebRTC, and invokes the local Hermes Core control
endpoint for actual work. Hermes Core remains responsible for session context,
tools, approvals, and privileged operations.

The current implementation is a local control-plane MVP. It does not join
Slack Huddles, publish a public endpoint, change an installed Slack app, or
start an API-billed Realtime session automatically.
