# -*- coding: utf-8 -*-
from __future__ import annotations

import secrets

import click

from ..app.auth import (
    _hash_password,
    _load_auth_data,
    _save_auth_data,
    is_auth_enabled,
)


@click.group("auth", help="Manage web authentication.")
def auth_group() -> None:
    """Manage web authentication."""


@auth_group.command("reset-password")
@click.option("--username", default="", help="Target username to reset.")
def reset_password_cmd(username: str) -> None:
    """Reset the password for a registered web user."""
    if not is_auth_enabled():
        click.echo(
            "Authentication is not enabled.\n"
            "Set QWENPAW_AUTH_ENABLED=true to enable it first.",
        )
        return

    data = _load_auth_data()

    if data.get("_auth_load_error"):
        raise click.ClickException(
            "Failed to read auth data. Check auth.json for corruption.",
        )

    users = data.get("users") or {}
    user = None
    if username:
        user = users.get(username)
        if user is None:
            raise click.ClickException(f"User '{username}' not found.")
    elif users:
        if len(users) == 1:
            user = next(iter(users.values()))
        else:
            raise click.ClickException(
                "Multiple users found. Please pass --username.",
            )
    else:
        user = data.get("user")

    if not user:
        click.echo("No registered user found. Nothing to reset.")
        return

    username = user.get("username", "<unknown>")
    click.echo(f"Resetting password for user: {username}")

    new_password = click.prompt(
        "New password",
        hide_input=True,
        confirmation_prompt=True,
    )

    if not new_password or not new_password.strip():
        raise click.ClickException("Password cannot be empty.")

    pw_hash, salt = _hash_password(new_password)
    if data.get("users") and username in data["users"]:
        data["users"][username]["password_hash"] = pw_hash
        data["users"][username]["password_salt"] = salt
    else:
        data["user"]["password_hash"] = pw_hash
        data["user"]["password_salt"] = salt

    # Invalidate existing tokens by rotating jwt_secret
    data["jwt_secret"] = secrets.token_hex(32)

    _save_auth_data(data)
    click.echo(
        "✓ Password reset successfully. "
        "All existing sessions have been invalidated.",
    )
