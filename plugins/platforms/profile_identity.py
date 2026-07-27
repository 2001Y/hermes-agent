"""Shared helpers for named platform profile identities.

The platform adapters own the transport semantics. This module only resolves the
active Hermes profile and safely selects configured string fields from
``PlatformConfig.extra``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence


def resolve_profile_name(metadata: Mapping[str, Any] | None = None) -> str:
    """Resolve the active Hermes profile for outbound platform presentation.

    Per-delivery metadata wins, followed by the process profile and finally the
    profile-shaped ``HERMES_HOME`` path. The fallback is ``default``.
    """
    if isinstance(metadata, Mapping):
        profile = str(metadata.get("profile") or "").strip()
        if profile:
            return profile

    profile = os.getenv("HERMES_PROFILE", "").strip()
    if profile:
        return profile

    hermes_home = os.getenv("HERMES_HOME", "").strip()
    if hermes_home:
        try:
            home_path = Path(hermes_home)
            if home_path.parent.name == "profiles" and home_path.name:
                return home_path.name
        except (OSError, ValueError):
            pass

    return "default"


def resolve_profile_identity(
    config_extra: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None = None,
    *,
    fields: Sequence[str],
) -> dict[str, str]:
    """Return configured identity fields for the active Hermes profile.

    Invalid or incomplete configuration returns an empty mapping. Adapters must
    treat presentation as best-effort and must not block message delivery.
    """
    if not isinstance(config_extra, Mapping):
        return {}
    identities = config_extra.get("profile_identities")
    if not isinstance(identities, Mapping):
        return {}

    identity = identities.get(resolve_profile_name(metadata))
    if not isinstance(identity, Mapping):
        return {}

    resolved: dict[str, str] = {}
    for field in fields:
        value = str(identity.get(field) or "").strip()
        if value:
            resolved[field] = value
    return resolved
