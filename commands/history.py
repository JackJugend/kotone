"""Read-only Discord view of Kotone's persistent change audit trail."""

from __future__ import annotations

import asyncio

import discord

from database import DB
from settings import GUILD_ID, resolve_aoty_username
from shared import configured_username_autocomplete


async def _configured_user_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    """Historia korzysta z tej samej lokalnej listy users co inne komendy."""

    return await configured_username_autocomplete(interaction, current, limit=25)


def _score_from_track(value):
    if isinstance(value, dict):
        return value.get("score")
    return value


def _event_text(event: dict) -> str:
    event_type = str(event.get("event_type") or "")
    old = event.get("old_value")
    new = event.get("new_value")
    item_key = str(event.get("item_key") or "").strip()

    if event_type == "rating_added":
        return f"⭐ Dodano ocenę **{new if new not in (None, '') else '—'}**"
    if event_type == "score_changed":
        return f"🔄 Ocena **{old if old not in (None, '') else '—'} → {new if new not in (None, '') else '—'}**"
    if event_type == "rating_removed":
        return f"🗑️ Usunięto ocenę **{old if old not in (None, '') else '—'}**"
    if event_type == "rating_restored":
        return f"♻️ Przywrócono ocenę **{new if new not in (None, '') else '—'}**"

    if event_type == "review_added":
        return "✎ Dodano recenzję"
    if event_type == "review_edited":
        return "✎ Edytowano recenzję"
    if event_type == "review_removed":
        return "✎ Usunięto recenzję"

    if event_type == "like_added":
        return "♥ Dodano like"
    if event_type == "like_removed":
        return "♡ Usunięto like"

    if event_type == "track_rating_added":
        return f"☰ **{item_key or 'Track'}** — dodano **{_score_from_track(new) or '—'}**"
    if event_type == "track_rating_changed":
        return (
            f"☰ **{item_key or 'Track'}** — "
            f"**{_score_from_track(old) or '—'} → {_score_from_track(new) or '—'}**"
        )
    if event_type == "track_rating_removed":
        return f"☰ **{item_key or 'Track'}** — usunięto **{_score_from_track(old) or '—'}**"
    if event_type == "track_ratings_added":
        return "☰ Dodano Track Ratings"
    if event_type == "track_ratings_removed":
        return "☰ Usunięto Track Ratings"

    if event_type == "favorites_changed":
        old_count = len(old) if isinstance(old, list) else 0
        new_count = len(new) if isinstance(new, list) else 0
        return f"♥ Zmieniono Favorites **({old_count} → {new_count})**"

    if event_type == "rating_distribution_changed":
        return "📊 Zmieniono Rating Distribution"

    if event_type == "avatar_changed":
        return "🖼️ Zmieniono avatar AOTY"

    if event_type == "profile_field_changed":
        names = {
            "ratings_count": "Ratings",
            "reviews_count": "Reviews",
            "lists_count": "Lists",
            "following_count": "Following",
            "followers_count": "Followers",
            "average_rating": "Average Rating",
            "average_rating_text": "Average Rating",
            "favorite_kind": "Favorites type",
        }
        field = str(event.get("field_name") or "Profil")
        label = names.get(field, field)
        return f"👤 {label}: **{old if old is not None else '—'} → {new if new is not None else '—'}**"

    return f"• {event_type.replace('_', ' ')}"


def _subject(event: dict) -> str | None:
    album = str(event.get("album") or "").strip()
    artist = str(event.get("artist") or "").strip()
    url = str(event.get("album_url") or "").strip()

    if not album and not artist:
        return None

    label = " — ".join(part for part in (artist, album) if part)
    if url:
        return f"[{label}]({url})"
    return label


def setup_history_command(tree: discord.app_commands.CommandTree):
    @tree.command(
        name="history",
        description="Pokazuje zapisane zmiany ocen, reviews, likes i Track Ratings",
    )
    @discord.app_commands.describe(
        username="Użytkownik z config.json",
        amount="Ile ostatnich zmian pokazać",
        category="Rodzaj zmian",
    )
    @discord.app_commands.autocomplete(username=_configured_user_autocomplete)
    @discord.app_commands.choices(
        category=[
            discord.app_commands.Choice(name="Wszystko", value="all"),
            discord.app_commands.Choice(name="Oceny", value="ratings"),
            discord.app_commands.Choice(name="Recenzje", value="reviews"),
            discord.app_commands.Choice(name="Likes", value="likes"),
            discord.app_commands.Choice(name="Track Ratings", value="tracks"),
            discord.app_commands.Choice(name="Profil + Favorites", value="profile"),
        ]
    )
    async def history_command(
        interaction: discord.Interaction,
        username: str | None = None,
        amount: discord.app_commands.Range[int, 1, 20] = 10,
        category: str = "all",
    ):
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        username = resolve_aoty_username(interaction.user.id, username)
        canonical = DB.canonical_username(username or "")
        if canonical is None:
            await interaction.response.send_message(
                "Historia jest zapisywana tylko dla użytkowników z `config.json`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        events = await asyncio.to_thread(
            DB.get_change_history,
            canonical,
            limit=int(amount),
            category=category,
        )

        if not events:
            await interaction.followup.send(
                f"Brak zapisanych zmian dla **{canonical}** w tej kategorii.",
                ephemeral=True,
            )
            return

        lines = []
        for event in events:
            try:
                stamp = int(float(event.get("detected_at") or 0))
            except (TypeError, ValueError):
                stamp = 0

            time_text = f"<t:{stamp}:R>" if stamp > 0 else "—"
            subject = _subject(event)
            change = _event_text(event)

            if subject:
                lines.append(f"{time_text} • {subject}\n{change}")
            else:
                lines.append(f"{time_text} • {change}")

        embed = discord.Embed(
            title=f"Historia zmian • {canonical}",
            description="\n\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text="SQLite • historia jest append-only; pierwszy sync tworzy baseline"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
