"""Komenda operatora do sterowania modułem Markova."""

from __future__ import annotations

import discord

from markov_service import MarkovService
from settings import is_operator_discord_id


def setup_markov_command(
    tree: discord.app_commands.CommandTree,
    service: MarkovService,
) -> None:
    @tree.command(name="markov", description="Włącza, wyłącza lub pokazuje stan Markova")
    @discord.app_commands.describe(action="Włącz, wyłącz albo pokaż statystyki")
    @discord.app_commands.choices(
        action=[
            discord.app_commands.Choice(name="Włącz", value="on"),
            discord.app_commands.Choice(name="Wyłącz", value="off"),
            discord.app_commands.Choice(name="Statystyki", value="stats"),
        ]
    )
    async def markov_command(interaction: discord.Interaction, action: str) -> None:
        if action in {"on", "off"}:
            if not is_operator_discord_id(getattr(interaction.user, "id", None)):
                await interaction.response.send_message(
                    "❌ Tylko operator Kotone może zmienić stan Markova.",
                    ephemeral=True,
                )
                return
            service.set_enabled(action == "on")

        stats = service.stats()
        state = "włączony" if stats["enabled"] else "wyłączony"
        await interaction.response.send_message(
            "**Markov Kotone**\n"
            f"• stan: **{state}**\n"
            f"• wiadomości: **{int(stats['messages']):,}**\n"
            f"• użytkownicy: **{int(stats['users']):,}**\n"
            f"• tokeny: **{int(stats['tokens']):,}**\n"
            f"• słowa: **{int(stats['words']):,}**\n"
            f"• przejścia: **{int(stats['transitions']):,}**\n"
            f"• kanał: <#{int(stats['channel_id'])}>"
            + ("\n• trwa początkowy import historii" if stats["bootstrap_running"] else ""),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
