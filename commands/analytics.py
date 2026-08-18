"""Interactive statistics based only on config-user data saved by Kotone."""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

import discord

from database import DB
from formats import RATING_FORMATS
from shared import score_color, username_autocomplete
from stats_cover_cache import load_cover_images
from stats_engine import compare, rating_distribution, summarize, wrapped
from stats_graphics import (
    render_compare,
    render_rating_distribution,
    render_stats,
    render_wrapped,
)
from views import TimedDisableView


BOT_DATABASE_FOOTER = "Komenda bazuje na bazie danych bota"
RATING_DISTRIBUTION_FORMATS = (
    ("all", "Wszystko"),
    ("tracks", "Oceny utworów"),
    *(
        (key, str(info["label"]))
        for key, info in RATING_FORMATS.items()
    ),
)


def _metric(value) -> str:
    return "—" if value is None else f"{float(value):.1f}"


def _new_embed(
    title: str,
    *,
    description: str | None = None,
    color=None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color.blurple(),
    )
    embed.set_footer(text=BOT_DATABASE_FOOTER)
    return embed


class RatingDistributionView(TimedDisableView):
    """Chart-first format selector backed by one in-memory SQLite snapshot."""

    def __init__(
        self,
        *,
        distributions: dict[str, dict],
        avatar_items: list[dict],
    ):
        super().__init__()
        self.distributions = distributions
        self.avatar_items = avatar_items
        self.active_category = "all"
        self.graphic_bytes: dict[str, bytes] = {}
        self.avatar_images: list[dict] | None = None
        self.example_images: dict[str, list[dict]] = {}
        self.render_lock = asyncio.Lock()

        self.selector = discord.ui.Select(
            placeholder="Wybierz format",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    default=(key == self.active_category),
                )
                for key, label in RATING_DISTRIBUTION_FORMATS
            ],
            custom_id="ratingdistribution:format",
            row=0,
        )
        self.selector.callback = self._select_format
        self.add_item(self.selector)

    def _mark_active(self, category: str) -> None:
        self.active_category = category
        for option in self.selector.options:
            option.default = option.value == category

    def _embed(self, category: str, filename: str) -> discord.Embed:
        data = self.distributions[category]
        embed = _new_embed(
            f"Rozkład ocen · {data['label']}",
            description=(
                f"**{data['ratings']}** ocen · średnia "
                f"**{_metric(data['average'])}** · mediana "
                f"**{_metric(data['median'])}**"
            ),
            color=score_color(data["average"]),
        )
        embed.set_image(url=f"attachment://{filename}")
        return embed

    async def render(self, category: str) -> bytes:
        cached = self.graphic_bytes.get(category)
        if cached is not None:
            return cached
        async with self.render_lock:
            cached = self.graphic_bytes.get(category)
            if cached is not None:
                return cached
            data = self.distributions[category]
            example_items = list(data.get("best_examples") or []) + list(
                data.get("worst_examples") or []
            )
            if self.avatar_images is None:
                self.avatar_images, example_images = await asyncio.gather(
                    asyncio.to_thread(
                        load_cover_images,
                        self.avatar_items,
                        limit=1,
                    ),
                    asyncio.to_thread(
                        load_cover_images,
                        example_items,
                        limit=4,
                    ),
                )
            else:
                example_images = await asyncio.to_thread(
                    load_cover_images,
                    example_items,
                    limit=4,
                )
            self.example_images[category] = example_images
            payload = dict(data)
            payload["_avatar_images"] = self.avatar_images
            payload["_example_images"] = example_images
            buffer = await asyncio.to_thread(render_rating_distribution, payload)
            content = buffer.getvalue()
            self.graphic_bytes[category] = content
            return content

    async def _select_format(self, interaction: discord.Interaction) -> None:
        category = self.selector.values[0]
        self._mark_active(category)
        await interaction.response.defer()
        try:
            content = await self.render(category)
            filename = f"ratingdistribution-{category}.png"
            await interaction.edit_original_response(
                embed=self._embed(category, filename),
                attachments=[discord.File(io.BytesIO(content), filename=filename)],
                view=self,
            )
        except Exception as exc:
            embed = _new_embed(
                "Rozkład ocen",
                description=(
                    "Nie udało się przygotować wykresu. "
                    f"`{type(exc).__name__}`"
                ),
            )
            await interaction.edit_original_response(
                embed=embed,
                attachments=[],
                view=self,
            )


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


def setup_analytics_commands(tree: discord.app_commands.CommandTree) -> None:
    async def genre_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ):
        username = str(getattr(interaction.namespace, "username", "") or "")
        needle = str(current or "").casefold()
        return [
            discord.app_commands.Choice(name=value[:100], value=value[:100])
            for value in DB.available_genres(username)
            if needle in value.casefold()
        ][:25]

    @tree.command(
        name="stats",
        description="Statystyki ocen użytkownika zapisane przez kotone.",
    )
    @discord.app_commands.describe(username="username")
    @discord.app_commands.autocomplete(username=username_autocomplete)
    async def stats_command(interaction: discord.Interaction, username: str):
        canonical = await _configured_user_or_error(interaction, username)
        if canonical is None:
            return
        await interaction.response.defer()
        rows, avatar = await asyncio.gather(
            asyncio.to_thread(DB.get_analytics_rows, canonical),
            asyncio.to_thread(DB.get_avatar, canonical),
        )
        data = summarize(canonical, rows)
        avatar_items = [
            {"username": canonical, "cover": avatar}
        ] if avatar else []
        cover_images, avatar_images = await asyncio.gather(
            asyncio.to_thread(
                load_cover_images,
                list(data.get("top_ratings") or []),
                limit=3,
            ),
            asyncio.to_thread(
                load_cover_images,
                avatar_items,
                limit=1,
            ),
        )
        data["_cover_images"] = cover_images
        data["_avatar_images"] = avatar_images
        graphic = await asyncio.to_thread(render_stats, data)
        await interaction.followup.send(
            file=discord.File(
                io.BytesIO(graphic.getvalue()),
                filename=f"stats-{canonical}.png",
            )
        )

    @tree.command(
        name="ratingdistribution",
        description="Graficzny rozkład ocen zapisanych przez Kotone",
    )
    @discord.app_commands.describe(
        username="Użytkownik",
        year="Rok",
        genre="Gatunek",
        score_min="Min rating 0–100",
        score_max="Max rating 0–100",
    )
    @discord.app_commands.autocomplete(
        username=username_autocomplete,
        genre=genre_autocomplete,
    )
    async def rating_distribution_command(
        interaction: discord.Interaction,
        username: str,
        year: int | None = None,
        genre: str | None = None,
        score_min: int | None = None,
        score_max: int | None = None,
    ):
        canonical = await _configured_user_or_error(interaction, username)
        if canonical is None:
            return
        current_year = datetime.now(UTC).year
        if year is not None and not 1900 <= year <= current_year + 1:
            await interaction.response.send_message(
                "Podaj poprawny rok wydania od 1900 do przyszłego roku.",
                ephemeral=True,
            )
            return
        if any(
            value is not None and not 0 <= value <= 100
            for value in (score_min, score_max)
        ):
            await interaction.response.send_message(
                "Zakres ocen musi mieścić się od 0 do 100.",
                ephemeral=True,
            )
            return
        if score_min is not None and score_max is not None and score_min > score_max:
            await interaction.response.send_message(
                "Minimalna ocena nie może być większa od maksymalnej.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        rows, track_rows, avatar = await asyncio.gather(
            asyncio.to_thread(DB.get_analytics_rows, canonical),
            asyncio.to_thread(DB.get_analytics_track_rows, canonical),
            asyncio.to_thread(DB.get_avatar, canonical),
        )
        score_range = (
            f"{score_min if score_min is not None else 0}–"
            f"{score_max if score_max is not None else 100}"
        )
        filter_parts = [
            str(year) if year is not None else "Wszystkie lata",
            genre or "Wszystkie gatunki",
            f"Oceny {score_range}",
        ]
        filter_text = " · ".join(filter_parts)
        distributions = {}
        for key, label in RATING_DISTRIBUTION_FORMATS:
            data = rating_distribution(
                canonical,
                rows,
                track_rows,
                key,
                category_label=label,
                year=year,
                genre=genre,
                score_min=score_min,
                score_max=score_max,
            )
            data["filter_text"] = f"{label} · {filter_text}"
            distributions[key] = data

        avatar_items = (
            [{"username": canonical, "cover": avatar}]
            if avatar
            else []
        )
        view = RatingDistributionView(
            distributions=distributions,
            avatar_items=avatar_items,
        )
        content = await view.render("all")
        filename = f"ratingdistribution-{canonical}-all.png"
        message = await interaction.followup.send(
            embed=view._embed("all", filename),
            file=discord.File(io.BytesIO(content), filename=filename),
            view=view,
            wait=True,
        )
        view.bind_message(message)

    @tree.command(
        name="compare",
        description="Porównuje oceny dwóch użytkowników.",
    )
    @discord.app_commands.describe(
        user_a="username",
        user_b="username",
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
                "Obaj użytkownicy muszą być zapisani w kotone.",
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
        rows_a, rows_b, avatar_a, avatar_b = await asyncio.gather(
            asyncio.to_thread(DB.get_analytics_rows, canonical_a),
            asyncio.to_thread(DB.get_analytics_rows, canonical_b),
            asyncio.to_thread(DB.get_avatar, canonical_a),
            asyncio.to_thread(DB.get_avatar, canonical_b),
        )
        data = compare(canonical_a, rows_a, canonical_b, rows_b)
        data["avatar_items"] = [
            {"username": username, "cover": avatar}
            for username, avatar in (
                (canonical_a, avatar_a),
                (canonical_b, avatar_b),
            )
            if avatar
        ]
        cover_items = data.get("shared_favorites") or data.get("disagreements") or []
        cover_images, avatar_images = await asyncio.gather(
            asyncio.to_thread(
                load_cover_images,
                list(cover_items),
                limit=3,
            ),
            asyncio.to_thread(
                load_cover_images,
                data["avatar_items"],
                limit=2,
            ),
        )
        data["_cover_images"] = cover_images
        data["_avatar_images"] = avatar_images
        graphic = await asyncio.to_thread(render_compare, data)
        await interaction.followup.send(
            file=discord.File(
                io.BytesIO(graphic.getvalue()),
                filename=f"compare-{canonical_a}-{canonical_b}.png",
            )
        )

    @tree.command(
        name="wrapped",
        description="Roczne podsumowanie ocen użytkownika zapisanych przez kotone",
    )
    @discord.app_commands.describe(
        username="username",
        year="Rok",
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
        rows, avatar = await asyncio.gather(
            asyncio.to_thread(DB.get_analytics_rows, canonical),
            asyncio.to_thread(DB.get_avatar, canonical),
        )
        data = wrapped(canonical, rows, selected_year)
        avatar_items = [
            {"username": canonical, "cover": avatar}
        ] if avatar else []
        cover_images, avatar_images = await asyncio.gather(
            asyncio.to_thread(
                load_cover_images,
                list(data.get("top_ratings") or []),
                limit=3,
            ),
            asyncio.to_thread(
                load_cover_images,
                avatar_items,
                limit=1,
            ),
        )
        data["_cover_images"] = cover_images
        data["_avatar_images"] = avatar_images
        graphic = await asyncio.to_thread(render_wrapped, data)
        await interaction.followup.send(
            file=discord.File(
                io.BytesIO(graphic.getvalue()),
                filename=f"wrapped-{canonical}-{selected_year}.png",
            )
        )
