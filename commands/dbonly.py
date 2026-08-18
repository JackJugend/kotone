"""Private operator switch for globally pausing background AOTY checks."""

from __future__ import annotations

import discord

from http_client import HTTP
from settings import AOTY_DB_ONLY_ADMIN_USER_ID, GUILD_ID


def _is_operator(interaction: discord.Interaction) -> bool:
    """Only the configured immutable Discord ID may change network policy."""

    return int(getattr(interaction.user, "id", 0) or 0) == AOTY_DB_ONLY_ADMIN_USER_ID


def setup_dbonly_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="dbonly",
        description="Przełącznik monitorowania AOTY w tle.",
    )
    @discord.app_commands.describe(mode="Stan sprawdzania AOTY")
    @discord.app_commands.choices(
        mode=[
            discord.app_commands.Choice(
                name="Włącz — zablokuj sprawdzanie AOTY",
                value="on",
            ),
            discord.app_commands.Choice(
                name="Wyłącz — monitor znów sprawdza AOTY",
                value="off",
            ),
            discord.app_commands.Choice(name="Pokaż status", value="status"),
        ]
    )
    async def dbonly_command(
        interaction: discord.Interaction,
        mode: str,
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        if not _is_operator(interaction):
            await interaction.response.send_message(
                "Nie masz uprawnień do `/dbonly`.",
                ephemeral=True,
            )
            return

        if mode == "status":
            enabled = HTTP.db_only_enabled()
        else:
            enabled = HTTP.set_db_only(
                mode == "on",
                actor=str(interaction.user.id),
            )

        if enabled:
            message = (
                "⏸ **Sprawdzanie AOTY jest zablokowane.** Monitor, archiwum "
                "i ręczne `/check` nie wyślą żadnego requestu. Komendy nadal "
                "czytają SQLite."
            )
        else:
            message = (
                "▶ **Sprawdzanie AOTY jest odblokowane.** Tylko monitor i "
                "worker w tle będą je wykonywać; komendy nadal korzystają "
                "z SQLite."
            )

        await interaction.response.send_message(message, ephemeral=True)
