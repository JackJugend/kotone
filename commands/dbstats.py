"""Discord diagnostics for Kotone's persistent SQLite cache."""

from __future__ import annotations

import asyncio

import discord

from database import DB
from settings import GUILD_ID, RATING_FORMATS


def _format_bytes(value: int | float | None) -> str:
    """Human-readable byte count without an external dependency."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        size = 0.0

    units = (
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    )

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"

        size /= 1024

    return "0 B"


def _discord_time(
    timestamp,
    *,
    fallback: str = "jeszcze nie",
) -> str:
    """Discord-native relative timestamp, e.g. '3 minuty temu'."""
    try:
        value = int(
            float(timestamp)
        )
    except (TypeError, ValueError):
        return fallback

    if value <= 0:
        return fallback

    return f"<t:{value}:R>"


def _safe_text(
    value,
    *,
    fallback: str = "—",
) -> str:
    if value in (
        None,
        "",
    ):
        return fallback

    return str(value)


def setup_dbstats_command(
    tree: discord.app_commands.CommandTree,
):
    @tree.command(
        name="dbstats",
        description="Pokazuje stan lokalnej bazy SQLite Kotone",
    )
    async def dbstats_command(
        interaction: discord.Interaction,
    ):
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        # quick_check + kilka COUNT(*) są lekkie, ale nie ma powodu blokować
        # event loopa Discorda nawet na ułamek sekundy.
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            stats = await asyncio.to_thread(
                DB.diagnostics
            )

        except Exception as exc:
            await interaction.followup.send(
                (
                    "❌ Nie udało się odczytać diagnostyki SQLite:\n"
                    f"`{type(exc).__name__}: {exc}`"
                ),
                ephemeral=True,
            )
            return

        counts = stats[
            "counts"
        ]

        healthy = bool(
            stats.get("healthy")
        )

        status_icon = (
            "✅"
            if healthy
            else "❌"
        )

        status_text = (
            "SQLite OK"
            if healthy
            else (
                "SQLite zgłasza problem: "
                + str(
                    stats.get(
                        "quick_check"
                    )
                    or "unknown"
                )
            )
        )

        embed = discord.Embed(
            title="Kotone • Database",
            description=(
                f"{status_icon} **{status_text}**\n"
                f"Schema: **v{stats.get('schema_version', '?')}**\n"
                f"Plik: `{stats.get('path', '—')}`"
            ),
            color=(
                discord.Color.green()
                if healthy
                else discord.Color.red()
            ),
        )

        embed.add_field(
            name="Stan danych",
            value=(
                f"👤 Users: **{counts['users']}**\n"
                f"⭐ Ratings: **{counts['ratings_active']}** aktywnych"
                f" / **{counts['ratings_total']}** zapisanych\n"
                f"💿 Releases cache: **{counts['releases']}**\n"
                f"🎵 Release tracks: **{counts['release_tracks']}**"
            ),
            inline=True,
        )

        embed.add_field(
            name="Szczegóły użytkowników",
            value=(
                f"✎ Reviews: **{counts['reviews']}**\n"
                f"↳ tekst recenzji w cache: **{counts['review_texts_cached']}**\n"
                f"☷ Albums z Track Ratings: **{counts['track_rating_albums']}**\n"
                f"↳ zapisane oceny utworów: **{counts['user_track_ratings']}**\n"
                f"♥ Favorites: **{counts['favorites']}**\n"
                f"↕ Historia zmian: **{counts['history']}**"
            ),
            inline=True,
        )

        embed.add_field(
            name="SQLite / Volume",
            value=(
                f"DB + WAL + SHM: **{_format_bytes(stats['disk_size'])}**\n"
                f"DB: **{_format_bytes(stats['database_size'])}**\n"
                f"WAL: **{_format_bytes(stats['wal_size'])}**\n"
                f"Backup: **{_format_bytes(stats['backup_size'])}**\n"
                f"Ostatni backup: **{_discord_time(stats['backup_mtime'])}**"
            ),
            inline=False,
        )

        total_formats = len(
            RATING_FORMATS
        )

        for user in stats.get(
            "users",
            [],
        ):
            archive_ok = int(
                user.get(
                    "archive_formats_ok"
                )
                or 0
            )

            archive_seen = int(
                user.get(
                    "archive_formats_seen"
                )
                or 0
            )

            # archive_ok/total_formats tells us how far the slow background
            # archival process has progressed. archive_seen can be useful
            # while a format currently has an error instead of a success.
            archive_line = (
                f"Archive: **{archive_ok}/{total_formats} formatów OK**"
            )

            if archive_seen > archive_ok:
                archive_line += (
                    f" ({archive_seen} rozpoczętych)"
                )

            value = (
                f"Ratings SQLite: **{user['ratings_active']}** aktywnych"
                f" / **{user['ratings_total']}** zapisanych\n"
                f"AOTY profile ratings: **"
                f"{_safe_text(user.get('profile_ratings_count'))}**\n"
                f"Reviews: **{user['reviews']}**"
                f" • Track Rating albums: **{user['track_rating_albums']}**\n"
                f"Track scores: **{user['track_rating_rows']}**"
                f" • Favorites: **{user['favorites']}**\n"
                f"{archive_line}\n"
                f"Archive items: **{user['archive_items']}**\n"
                f"Profile sync: **{_discord_time(user.get('profile_synced_at'))}**\n"
                f"Ratings sync: **{_discord_time(user.get('ratings_synced_at'))}**"
            )

            last_error = user.get(
                "last_error"
            )

            if last_error:
                clipped = str(
                    last_error
                ).replace(
                    "\n",
                    " ",
                )[:300]

                value += (
                    f"\n⚠️ Last error: `{clipped}` "
                    f"({_discord_time(user.get('last_error_at'))})"
                )

            embed.add_field(
                name=f"👤 {user['username']}",
                value=value,
                inline=False,
            )

        embed.set_footer(
            text=(
                "Tylko dane użytkowników wpisanych w config.json • "
                "wynik widoczny tylko dla Ciebie"
            )
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )
