"""Komenda /last renderująca ostatnią pasującą ocenę z bazy Kotone."""

import discord
import requests

import aoty
from formats import RATING_FORMATS
from services import DATA
from shared import (
    aoty_score_or_missing,
    load_release_variables,
    must_hear_title_marker,
    rating_flags_text,
    release_year_suffix,
    score_color,
    set_aoty_footer,
    score_or_nr,
    username_autocomplete,
)
from settings import resolve_aoty_username
from views import SingleRatingView


def _safe_link_label(value: object) -> str:
    """Zachowaj czytelną nazwę wewnątrz linku Markdown Discorda."""

    return str(value or "—").replace("[", "\\[").replace("]", "\\]")


def _release_header_links(variables) -> str:
    """Pokaż artystę i album jako dwa osobne linki w ``/last``.

    Tytuł embeda Discorda obsługuje tylko jeden URL, przez co oba napisy
    prowadziły wcześniej do albumu. Nagłówek Markdown zachowuje jeden wiersz,
    ale pozwala dać każdemu z nich właściwy adres.
    """

    artist = _safe_link_label(variables.display_artist)
    album = _safe_link_label(variables.display_album)
    if variables.artist_url:
        artist = f"[{artist}]({variables.artist_url})"
    if variables.album_url or variables.url:
        album = f"[**{album}**]({variables.album_url or variables.url})"
    else:
        album = f"**{album}**"
    must_hear = must_hear_title_marker(variables)
    separator = f" — {must_hear} " if must_hear else " — "
    return f"{artist}{separator}{album}{release_year_suffix(variables.year)}"


def _apply_rating_detail(item: dict, detail: dict) -> None:
    """Przenieś do wybranej oceny tylko flagi i kanoniczny URL recenzji."""

    if detail.get("review_url"):
        item["review_url"] = detail["review_url"]
    for key in ("has_review", "has_track_ratings", "liked"):
        if detail.get(key):
            item[key] = True


async def _preload_rating_detail(username: str, item: dict) -> dict | None:
    """Wczytaj szczegóły oceny z SQLite bez interaktywnego HTTP."""

    try:
        detail = await DATA.get_user_rating_for_album(
            username,
            item.get("album_id"),
            item.get("url"),
            item.get("release_format"),
            fallback_limit=10,
            user_release_url=item.get("review_url"),
            album_title=item.get("album"),
            require_detail=True,
            allow_network=False,
        )
    except aoty.AOTYRateLimit:
        return None
    except Exception as exc:
        print(
            f"[LAST] Nie udało się wstępnie pobrać szczegółów "
            f"{username} / {item.get('album_id')}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None

    _apply_rating_detail(item, detail)
    return detail if not detail.get("detail_incomplete") else None


async def _cached_avatar(username: str) -> str | None:
    """Avatar AOTY jest opcjonalny i nie może blokować `/last`."""

    try:
        return await DATA.get_avatar(username)
    except Exception:
        return None


def _build_last_embed(
    username: str,
    variables,
    item: dict,
    avatar: str | None,
    *,
    score_display: str | None = None,
) -> discord.Embed:
    """Zbuduj kartę Home wspólną dla ``/last`` i powiadomień monitora."""

    flags = rating_flags_text(item)
    last_flags = f" {flags}" if flags else ""
    description_lines = [
        f"## {_release_header_links(variables)}",
        f"# {score_display or score_or_nr(variables.score)} {last_flags}",
    ]
    if variables.genres:
        description_lines.append(variables.all_genres_text.title())
    if variables.secondary_genres:
        description_lines.append(f"*{variables.secondary_genres_text.title()}*")
    if variables.vibes:
        description_lines.append(f"-# {variables.vibes_text}")

    embed = discord.Embed(
        description="\n".join(description_lines),
        color=score_color(variables.score),
    )
    embed.add_field(
        name=(
            f"<:aoty:1539095897084924004> "
            f"**{aoty_score_or_missing(variables.aoty_user_score, variables.ratings_count)}**"
        ),
        value=f"/{variables.ratings_count}",
        inline=True,
    )
    embed.add_field(
        name=f"\\🏆 **{variables.year_ranking_text}**",
        value=f"for **{variables.year}**",
        inline=True,
    )
    author = f"{username}  •  {variables.date}"
    if avatar:
        embed.set_author(
            name=author,
            url=f"https://www.albumoftheyear.org/user/{username}",
            icon_url=avatar,
        )
    else:
        embed.set_author(name=author)
    if variables.cover:
        embed.set_thumbnail(url=variables.cover)
    set_aoty_footer(
        embed,
        f"{variables.album_format}  •  {variables.release_date}  •  "
        f"{variables.labels_text}",
    )
    return embed


def setup_last_command(tree: discord.app_commands.CommandTree):
    format_choices = [
        discord.app_commands.Choice(name="Wszystkie formaty", value="all")
    ] + [
        discord.app_commands.Choice(name=info["label"], value=key)
        for key, info in RATING_FORMATS.items()
    ]

    async def genre_autocomplete(interaction: discord.Interaction, current: str):
        username = str(getattr(interaction.namespace, "username", "") or "")
        needle = str(current or "").casefold()
        return [
            discord.app_commands.Choice(name=value[:100], value=value[:100])
            for value in DATA.cached_genres(username)
            if needle in value.casefold()
        ][:25]

    @tree.command(
        name="last",
        description="Pokazuje ostatnią ocenę użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Użytkownik AOTY",
        format="Opcjonalnie: tylko konkretny format wydania",
        genre="Gatunek zapisany w kotone",
        year="Rok wydania",
        decade="Początek dekady, np. 2020",
        rating_date="Data oceny, np. 01.05.2026",
        aoty_min="Minimalny AOTY User Score",
        aoty_max="Maksymalny AOTY User Score",
        user_min="Minimalna ocena użytkownika",
        user_max="Maksymalna ocena użytkownika",
    )
    @discord.app_commands.autocomplete(
        username=username_autocomplete,
        genre=genre_autocomplete,
    )
    @discord.app_commands.choices(format=format_choices)
    async def last_command(
        interaction: discord.Interaction,
        username: str | None = None,
        format: str = "all",
        genre: str | None = None,
        year: int | None = None,
        decade: int | None = None,
        rating_date: str | None = None,
        aoty_min: int | None = None,
        aoty_max: int | None = None,
        user_min: int | None = None,
        user_max: int | None = None,
    ):
        await interaction.response.defer()
        username = resolve_aoty_username(interaction.user.id, username)
        if not username:
            await interaction.followup.send(
                "❌ Wpisz `username` albo wywołaj tę komendę z konta "
                "użytkownika Kotone w `config.json`.",
                ephemeral=True,
            )
            return

        try:
            if not await DATA.user_exists(username):
                await interaction.followup.send(
                    f"❌ Konto AOTY **{username}** nie istnieje."
                )
                return

            ratings = await DATA.get_recent_ratings(
                username,
                1,
                format,
                allow_network=False,
                genre=genre,
                year=year,
                decade=decade,
                rating_date=rating_date,
                aoty_min=aoty_min,
                aoty_max=aoty_max,
                user_min=user_min,
                user_max=user_max,
            )

        except aoty.AOTYRateLimit:
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań."
            )
            return
        except requests.RequestException as exc:
            await interaction.followup.send(
                f"❌ Błąd połączenia z AOTY: `{exc}`"
            )
            return
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Błąd: `{type(exc).__name__}: {exc}`"
            )
            return

        if not ratings:
            selected = (
                RATING_FORMATS.get(format, {}).get("label")
                if format != "all"
                else None
            )
            suffix = f" w formacie **{selected}**" if selected else ""
            await interaction.followup.send(
                f"❌ Nie znaleziono ocen użytkownika **{username}**{suffix}."
            )
            return

        latest = ratings[0]

        variables = await load_release_variables(
            latest,
            username=username,
            missing="—",
        )

        if variables.artist_url:
            latest["artist_url"] = variables.artist_url

        if variables.album_url:
            latest["url"] = variables.album_url

        live_extra = await _preload_rating_detail(username, latest)
        avatar = await _cached_avatar(username)
        embed = _build_last_embed(username, variables, latest, avatar)

        view = SingleRatingView(
            username=username,
            item=latest,
            main_embed=embed,
            extra=(
                live_extra
                if (
                    live_extra
                    and not live_extra.get(
                        "detail_incomplete"
                    )
                )
                else None
            ),
            artist_url=variables.artist_url or None,
            album_url=variables.album_url or variables.url or None,
            author_icon_url=avatar,
            owner_id=interaction.user.id,
        )

        message = await interaction.followup.send(
            embed=embed,
            view=view,
            wait=True,
        )
        view.bind_message(message)
