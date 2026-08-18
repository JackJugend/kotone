"""SQLite-only public statistics commands with locally rendered PNG cards."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import discord

from database import DB
from shared import username_autocomplete
from stats_engine import compare, summarize, wrapped


def _metric(value) -> str:
    return "—" if value is None else f"{float(value):.1f}"


def _pairs(items: list[tuple[str, int]], *, empty: str = "—") -> str:
    if not items:
        return empty
    return "\n".join(f"**{name}** · {count}" for name, count in items)


def _top_ratings(items: list[dict]) -> str:
    if not items:
        return "—"
    return "\n".join(
        f"**{item['score']:.0f}** · {item['artist']} — {item['album']}"
        for item in items
    )[:1024]


def _comparison_items(items: list[dict], user_a: str, user_b: str) -> str:
    if not items:
        return "—"
    return "\n".join(
        f"**Δ {item['gap']:.0f}** · {item['artist']} — {item['album']} "
        f"({user_a} {item['score_a']:.0f} / {user_b} {item['score_b']:.0f})"
        for item in items
    )[:1024]


async def _configured_user_or_error(
    interaction: discord.Interaction,
    username: str,
) -> str | None:
    canonical = DB.canonical_username(username)
    if canonical is not None:
        return canonical
    await interaction.response.send_message(
        "Ta komenda obsługuje wyłącznie użytkowników wpisanych w `config.json`.",
        ephemeral=True,
    )
    return None


async def _send_graphic(
    interaction: discord.Interaction,
    embed: discord.Embed,
    renderer,
    data: dict,
    filename: str,
) -> None:
    try:
        buffer = await asyncio.to_thread(renderer, data)
        file = discord.File(buffer, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        await interaction.followup.send(embed=embed, file=file)
    except Exception as exc:
        # The numerical result is more important than optional artwork. Keep
        # the command useful if a deployment is ever missing a font/backend.
        embed.set_footer(
            text=f"SQLite • grafika niedostępna: {type(exc).__name__}"
        )
        await interaction.followup.send(embed=embed)


def setup_analytics_commands(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="stats",
        description="Statystyki ocen użytkownika zapisane w SQLite",
    )
    @discord.app_commands.describe(username="Użytkownik z configu")
    @discord.app_commands.autocomplete(username=username_autocomplete)
    async def stats_command(interaction: discord.Interaction, username: str):
        canonical = await _configured_user_or_error(interaction, username)
        if canonical is None:
            return
        await interaction.response.defer()
        rows = await asyncio.to_thread(DB.get_analytics_rows, canonical)
        data = summarize(canonical, rows)

        embed = discord.Embed(
            title=f"📊 Statystyki • {canonical}",
            description=(
                f"**{data['ratings']}** ocen · średnia **{_metric(data['average'])}** "
                f"· mediana **{_metric(data['median'])}**\n"
                f"✎ **{data['reviews']}** · ❤︎⁠ **{data['likes']}** · "
                f"☰ **{data['track_albums']}** albumów / **{data['track_scores']}** ocen utworów"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Najczęstsze gatunki", value=_pairs(data["top_genres"]), inline=True)
        embed.add_field(name="Najczęstsze formaty", value=_pairs(data["top_formats"]), inline=True)
        embed.add_field(name="Najczęściej oceniani artyści", value=_pairs(data["top_artists"]), inline=True)
        embed.add_field(name="Najwyższe oceny", value=_top_ratings(data["top_ratings"]), inline=False)
        embed.set_footer(text="Tylko SQLite • tylko użytkownicy z configu • 0 requestów HTTP")

        from stats_graphics import render_stats

        await _send_graphic(interaction, embed, render_stats, data, f"stats-{canonical}.png")

    @tree.command(
        name="compare",
        description="Porównuje oceny dwóch użytkowników z configu",
    )
    @discord.app_commands.describe(
        user_a="Pierwszy użytkownik z configu",
        user_b="Drugi użytkownik z configu",
    )
    @discord.app_commands.autocomplete(
        user_a=username_autocomplete,
        user_b=username_autocomplete,
    )
    async def compare_command(
        interaction: discord.Interaction,
        user_a: str,
        user_b: str,
    ):
        canonical_a = DB.canonical_username(user_a)
        canonical_b = DB.canonical_username(user_b)
        if canonical_a is None or canonical_b is None:
            await interaction.response.send_message(
                "Obaj użytkownicy muszą być wpisani w `config.json`.",
                ephemeral=True,
            )
            return
        if canonical_a.casefold() == canonical_b.casefold():
            await interaction.response.send_message(
                "Wybierz dwóch różnych użytkowników.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        rows_a, rows_b = await asyncio.gather(
            asyncio.to_thread(DB.get_analytics_rows, canonical_a),
            asyncio.to_thread(DB.get_analytics_rows, canonical_b),
        )
        data = compare(canonical_a, rows_a, canonical_b, rows_b)
        agreement = _metric(data["agreement"])
        gap = _metric(data["mean_gap"])
        embed = discord.Embed(
            title=f"⚖️ {canonical_a} × {canonical_b}",
            description=(
                f"Wspólne oceny: **{data['common_count']}**\n"
                f"Średnie: **{canonical_a} {_metric(data['average_a'])}** · "
                f"**{canonical_b} {_metric(data['average_b'])}**\n"
                f"Zgodność: **{agreement}%** · średnia różnica: **{gap}**"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Największe różnice",
            value=_comparison_items(data["disagreements"], canonical_a, canonical_b),
            inline=False,
        )
        embed.add_field(
            name="Wspólne najwyżej ocenione",
            value=_comparison_items(data["shared_favorites"], canonical_a, canonical_b),
            inline=False,
        )
        embed.set_footer(text="Zgodność = 100 − średnia bezwzględna różnica • tylko SQLite")

        from stats_graphics import render_compare

        await _send_graphic(
            interaction,
            embed,
            render_compare,
            data,
            f"compare-{canonical_a}-{canonical_b}.png",
        )

    @tree.command(
        name="wrapped",
        description="Roczne podsumowanie ocen użytkownika z SQLite",
    )
    @discord.app_commands.describe(
        username="Użytkownik z configu",
        year="Rok dodania ocen; domyślnie bieżący",
    )
    @discord.app_commands.autocomplete(username=username_autocomplete)
    async def wrapped_command(
        interaction: discord.Interaction,
        username: str,
        year: int | None = None,
    ):
        canonical = await _configured_user_or_error(interaction, username)
        if canonical is None:
            return
        selected_year = year if year is not None else datetime.now(UTC).year
        if not 1900 <= selected_year <= datetime.now(UTC).year + 1:
            await interaction.response.send_message(
                "Podaj poprawny rok od 1900 do przyszłego roku.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        rows = await asyncio.to_thread(DB.get_analytics_rows, canonical)
        data = wrapped(canonical, rows, selected_year)
        embed = discord.Embed(
            title=f"🎁 Wrapped {selected_year} • {canonical}",
            description=(
                f"**{data['ratings']}** ocen · średnia **{_metric(data['average'])}** "
                f"· mediana **{_metric(data['median'])}**\n"
                f"✎ **{data['reviews']}** · ❤︎⁠ **{data['likes']}** · "
                f"☰ **{data['track_albums']}**"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Gatunki roku", value=_pairs(data["top_genres"]), inline=True)
        embed.add_field(name="Artyści roku", value=_pairs(data["top_artists"]), inline=True)
        embed.add_field(name="Najwyższe oceny", value=_top_ratings(data["top_ratings"]), inline=False)
        embed.set_footer(text="Rok dotyczy daty dodania oceny • tylko SQLite")

        from stats_graphics import render_wrapped

        await _send_graphic(
            interaction,
            embed,
            render_wrapped,
            data,
            f"wrapped-{canonical}-{selected_year}.png",
        )
