"""Tests for Matrix account profile identity configuration and application."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, load_gateway_config
from plugins.platforms.matrix.adapter import (
    _apply_matrix_profile_identity,
    _apply_matrix_profile_identity_standalone,
    _apply_yaml_config,
    resolve_matrix_profile_identity,
)


def test_resolves_identity_for_active_profile(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "night")
    extra = {
        "profile_identities": {
            "default": {"display_name": "Default"},
            "night": {"display_name": "Hermes Night", "avatar": "mxc://example/avatar"},
        }
    }

    assert resolve_matrix_profile_identity(extra) == {
        "display_name": "Hermes Night",
        "avatar": "mxc://example/avatar",
    }


def test_invalid_identity_config_is_ignored(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert resolve_matrix_profile_identity({"profile_identities": []}) == {}
    assert (
        resolve_matrix_profile_identity({
            "profile_identities": {"default": "not-a-mapping"}
        })
        == {}
    )


def test_yaml_config_keeps_profile_identities_structured(monkeypatch):
    monkeypatch.delenv("MATRIX_REQUIRE_MENTION", raising=False)
    result = _apply_yaml_config(
        {},
        {
            "profile_identities": {
                "default": {"display_name": "Hermes", "avatar": "mxc://hs/avatar"},
                "invalid": "ignored",
            }
        },
    )

    assert result == {
        "profile_identities": {
            "default": {"display_name": "Hermes", "avatar": "mxc://hs/avatar"}
        }
    }


def test_load_gateway_config_bridges_matrix_profile_identities(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "matrix:\n"
        "  profile_identities:\n"
        "    default:\n"
        "      display_name: Hermes\n"
        "      avatar: mxc://hs/avatar\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    config = load_gateway_config()

    assert config.platforms[Platform.MATRIX].extra["profile_identities"] == {
        "default": {"display_name": "Hermes", "avatar": "mxc://hs/avatar"}
    }


@pytest.mark.asyncio
async def test_applies_display_name_and_mxc_avatar():
    client = SimpleNamespace(
        set_displayname=AsyncMock(),
        set_avatar_url=AsyncMock(),
    )

    await _apply_matrix_profile_identity(
        client,
        {"display_name": "Hermes", "avatar": "mxc://hs/avatar"},
    )

    client.set_displayname.assert_awaited_once_with("Hermes", check_current=True)
    client.set_avatar_url.assert_awaited_once_with(
        "mxc://hs/avatar", check_current=True
    )


@pytest.mark.asyncio
async def test_uploads_local_avatar_before_setting_it(tmp_path):
    avatar = tmp_path / "hermes.png"
    avatar.write_bytes(b"png-bytes")
    client = SimpleNamespace(
        set_displayname=AsyncMock(),
        upload_media=AsyncMock(return_value="mxc://hs/uploaded"),
        set_avatar_url=AsyncMock(),
    )

    await _apply_matrix_profile_identity(
        client,
        {"display_name": "Hermes", "avatar": str(avatar)},
    )

    client.upload_media.assert_awaited_once_with(
        b"png-bytes",
        mime_type="image/png",
        filename="hermes.png",
        size=9,
    )
    client.set_avatar_url.assert_awaited_once_with(
        "mxc://hs/uploaded", check_current=True
    )


class _Response:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}

    async def text(self):
        return ""

    async def json(self):
        return self._payload


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self):
        self.calls = []

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return _ResponseContext(_Response())

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _ResponseContext(_Response(payload={"content_uri": "mxc://hs/uploaded"}))


@pytest.mark.asyncio
async def test_standalone_profile_identity_updates_account_and_uploads_avatar(tmp_path):
    avatar = tmp_path / "hermes.png"
    avatar.write_bytes(b"png-bytes")
    session = _Session()

    await _apply_matrix_profile_identity_standalone(
        session,
        "https://matrix.example.org",
        "syt_test_token",
        "@hermes:example.org",
        {"display_name": "Hermes", "avatar": str(avatar)},
    )

    assert [method for method, _url, _kwargs in session.calls] == [
        "PUT",
        "POST",
        "PUT",
    ]
    assert session.calls[0][1].endswith(
        "/_matrix/client/v3/profile/%40hermes%3Aexample.org/displayname"
    )
    assert session.calls[0][2]["json"] == {"displayname": "Hermes"}
    assert session.calls[1][2]["data"] == b"png-bytes"
    assert session.calls[2][1].endswith(
        "/_matrix/client/v3/profile/%40hermes%3Aexample.org/avatar_url"
    )
    assert session.calls[2][2]["json"] == {"avatar_url": "mxc://hs/uploaded"}
