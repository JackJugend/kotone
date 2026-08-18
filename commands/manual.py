"""Temporary manual review/like edits for owned config-user ratings."""

from __future__ import annotations

import asyncio

import discord

from commands.rating_import import IMPORT_USERS_BY_DISCORD_ID
from database import DB
from rating_import import normalized_text
from settings import GUILD_ID


def _owned_username(interaction: discord.Interaction) -> str | None:
    discord_user_id = int(getattr(interaction.user, "id", 0) or 0)
    username = IMPORT_USERS_BY_DISCORD_ID.get(discord_user_id)
    return DB.canonical_username(username) if username else None


async def manual_album_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    username = _owned_username(interaction)
    if username is None:
        return []
    rows = await asyncio.to_thread(DB.get_analytics_rows, username)
    needle = normalized_text(current)
    choices = []
    seen = set()
    for row in rows:
        album_id = str(row.get("album_id") or "").strip()
        if not album_id or album_id in seen:
            continue
        label = " — ".join(
            value
            for value in (
                str(row.get("artist") or "").strip(),
                str(row.get("album") or "").strip(),
            )
            if value
        )
        if needle and needle not in normalized_text(label):
            continue
        seen.add(album_id)
        choices.append(
            discord.app_commands.Choice(
                name=(label or f"Album #{album_id}")[:100],
                value=album_id[:100],
            )
        )
        if len(choices) >= 25:
            break
    return choices


def setup_manual_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="manual",
        description="Tymczasowo ustawia review lub like na Twoim profilu kotone.",
    )
    @discord.app_commands.describe(
        album="Album z Twojego archiwum bota",
        action="Ręczna zmiana do kolejnej pełnej synchronizacji AOTY",
        review="Treść wymagana tylko dla opcji ustawienia recenzji",
    )
    @discord.app_commands.autocomplete(album=manual_album_autocomplete)
    @discord.app_commands.choices(
        action=[
            discord.app_commands.Choice(name="❤︎ Dodaj like", value="like_on"),
            discord.app_commands.Choice(name="♡ Usuń like", value="like_off"),
            discord.app_commands.Choice(name="✎ Ustaw/edytuj review", value="review_set"),
            discord.app_commands.Choice(name="✎ Usuń review", value="review_remove"),
        ]
    )
    async def manual_command(
        interaction: discord.Interaction,
        album: str,
        action: str,
        review: str | None = None,
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return
        username = _owned_username(interaction)
        if username is None:
            await interaction.response.send_message(
                "Nie masz przypisanego użytkownika z `config.json`.",
                ephemeral=True,
            )
            return
        if action == "review_set" and not str(review or "").strip():
            await interaction.response.send_message(
                "Dla opcji ustawienia review podaj jego treść.",
                ephemeral=True,
            )
            return
        if action != "review_set" and review:
            await interaction.response.send_message(
                "Pole `review` jest używane tylko przez opcję ustawienia recenzji.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(DB.backup_if_due, force=True)
            result = await asyncio.to_thread(
                DB.manual_update_rating_detail,
                username,
                album,
                action,
                review_text=review,
            )
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Ręczna zmiana nie powiodła się: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return

        subject = " — ".join(
            value
            for value in (
                str(result.get("artist") or "").strip(),
                str(result.get("album") or "").strip(),
            )
            if value
        ) or f"Album #{result['album_id']}"
        state = {
            "like_on": "❤︎ like dodany",
            "like_off": "♡ like usunięty",
            "review_set": "✎ review zapisane",
            "review_remove": "✎ review usunięte",
        }[action]
        change_note = "Zapisano zmianę." if result["changed"] else "Taki stan był już zapisany."
        await interaction.followup.send(
            f"✅ **{subject}** — {state}.\n{change_note} "
            "Pełna synchronizacja AOTY w przyszłości pozostaje autorytatywna "
            "i może nadpisać tę wartość.",
            ephemeral=True,
        )
