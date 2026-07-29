"""Helpers for expanding user-defined quick-command aliases."""

from __future__ import annotations


def build_alias_command(
    target: object,
    user_args: object = "",
    default_args: object = "",
) -> str:
    """Build the slash command produced by a configured alias.

    ``default_args`` are used only when the caller supplies no arguments.
    This lets an alias provide safe defaults (for example, ``--once``) while
    still allowing an explicit invocation such as ``--session`` to replace
    them.
    """
    target_text = str(target or "").strip()
    if not target_text:
        return ""
    if not target_text.startswith("/"):
        target_text = f"/{target_text}"

    user_args_text = str(user_args or "").strip()
    suffix = user_args_text or str(default_args or "").strip()
    return f"{target_text} {suffix}".strip()
