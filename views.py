"""Reusable Discord components for reviews, track ratings and profile paging."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import discord

import aoty
from display_utils import display_romanized_name
from shared import rating_flags_text, score_color, score_icon


def _trim_description(text: str, limit: int = 4000) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"



def build_review_embed(username: str, item: dict, extra: dict) -> discord.Embed:
    artist = display_romanized_name(item.get("artist") or "Nieznany artysta")
    album = display_romanized_name(item.get("album") or item.get("title") or "Nieznane wydanie")
    score = extra.get("score") or item.get("score")
    review_text = extra.get("review_text") or "Brak recenzji."

    embed = discord.Embed(
        title=f"✎ {artist} — {album}",
        url=extra.get("review_url") or item.get("url"),
        description=_trim_description(review_text),
        color=score_color(score),
    )

    embed.set_author(
        name=f"{username}  •  {extra.get('date') or item.get('date') or '?'}",
        url=f"https://www.albumoftheyear.org/user/{username}/",
    )

    cover = item.get("cover")
    if cover:
        embed.set_thumbnail(url=cover)

    embed.set_footer(text=f"AOTY • {score_icon(score)} {score or 'NR'}")
    return embed


def build_track_ratings_embed(username: str, item: dict, extra: dict) -> discord.Embed:
    artist = display_romanized_name(item.get("artist") or "Nieznany artysta")
    album = display_romanized_name(item.get("album") or item.get("title") or "Nieznane wydanie")
    score = extra.get("score") or item.get("score")
    track_ratings = list(extra.get("track_ratings") or [])

    lines = []
    for track in track_ratings:
        number = track.get("number") or "?"
        title = track.get("title") or "Nieznany utwór"
        track_score = track.get("score") or "NR"
        lines.append(f"**{number}.** {title} — **{track_score}**")

    description = "\n".join(lines) if lines else "Brak ocen tracklisty."

    embed = discord.Embed(
        title=f"☷ {artist} — {album}",
        url=extra.get("review_url") or item.get("url"),
        description=_trim_description(description),
        color=score_color(score),
    )

    embed.set_author(
        name=f"{username}  •  {extra.get('date') or item.get('date') or '?'}",
        url=f"https://www.albumoftheyear.org/user/{username}/",
    )

    cover = item.get("cover")
    if cover:
        embed.set_thumbnail(url=cover)

    embed.set_footer(text=f"AOTY • {score_icon(score)} {score or 'NR'}")
    return embed


VIEW_TIMEOUT_SECONDS = 15 * 60


class TimedDisableView(discord.ui.View):
    """A view that visibly disables every control after 15 minutes."""

    def __init__(
        self,
        *,
        timeout: float = VIEW_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=timeout)
        self.message = None

    def bind_message(self, message) -> None:
        """Remember the sent Discord message so on_timeout can edit it."""
        self.message = message

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

        if self.message is None:
            return

        try:
            await self.message.edit(
                view=self
            )
        except Exception:
            # Deleted message / missing permission / expired webhook token:
            # controls are still dead server-side because the View timed out.
            pass


async def _load_live_extra(
    username: str,
    item: dict,
    *,
    fallback_limit: int | None = 60,
) -> dict:
    """Load review / track ratings without letting AOTY 429 crash a View.

    For /last, /recent and /profile the selected rating is recent, so a small
    fallback limit is enough if the direct user-release URL cannot be used.
    """
    try:
        return await asyncio.to_thread(
            aoty.get_user_rating_for_album,
            username,
            item.get("album_id"),
            item.get("url"),
            item.get("release_format") or item.get("album_format"),
            fallback_limit,
            item.get("review_url"),
            item.get("album") or item.get("title"),
        )

    except aoty.AOTYRateLimit as exc:
        return {
            "score": item.get("score"),
            "date": item.get("date"),
            "review_url": item.get("review_url"),
            "review_text": None,
            "has_review": bool(item.get("has_review")),
            "track_ratings": [],
            "has_track_ratings": bool(
                item.get("has_track_ratings")
            ),
            "liked": bool(item.get("liked")),
            "rate_limited": True,
            "rate_limit_error": str(exc),
        }




def _has_review_available(
    item: dict | None,
    extra: dict | None = None,
) -> bool:
    """True tylko wtedy, gdy ocena ma recenzję."""
    item = item or {}

    if (
        extra is not None
        and not extra.get("rate_limited")
        and not extra.get("detail_incomplete")
    ):
        return bool(
            extra.get("review_text")
            or extra.get("has_review")
        )

    return bool(
        item.get("review_text")
        or item.get("has_review")
    )


def _has_track_ratings_available(
    item: dict | None,
    extra: dict | None = None,
) -> bool:
    """True tylko wtedy, gdy ocena ma przynajmniej jeden Track Rating."""
    item = item or {}

    if (
        extra is not None
        and not extra.get("rate_limited")
        and not extra.get("detail_incomplete")
    ):
        track_ratings = list(
            extra.get("track_ratings")
            or []
        )

        has_actual_score = any(
            track.get("score") not in (
                None,
                "",
                "NR",
            )
            for track in track_ratings
        )

        return bool(
            has_actual_score
            or extra.get("has_track_ratings")
        )

    return bool(
        item.get("track_ratings")
        or item.get("has_track_ratings")
    )


def _set_button_visible(
    view: discord.ui.View,
    button: discord.ui.Item,
    visible: bool,
) -> None:
    """Ukrywa button całkowicie zamiast tylko go disable'ować."""
    present = button in view.children

    if visible and not present:
        view.add_item(button)

    elif not visible and present:
        view.remove_item(button)



class RatingDetailsMixin:
    username: str
    item: dict
    _extra_cache: dict | None

    async def _load_extra(self) -> dict:
        if self._extra_cache is not None:
            return self._extra_cache

        extra = await _load_live_extra(
            self.username,
            self.item,
            fallback_limit=10,
        )

        # Do not cache a temporary 429 response; the user may retry later.
        if (
            not extra.get("rate_limited")
            and not extra.get("detail_incomplete")
        ):
            self._extra_cache = extra

        return extra


class SingleRatingView(TimedDisableView, RatingDetailsMixin):
    """Interactive tabs for one rating.

    /last can additionally provide:
      - Szczegóły
      - Tracklista
      - direct Artysta / Album URL buttons

    Other places that use SingleRatingView keep working because all extended
    arguments are optional.
    """

    def __init__(
        self,
        *,
        username: str,
        item: dict,
        main_embed: discord.Embed,
        extra: dict | None = None,
        details_embed: discord.Embed | None = None,
        tracklist_embed: discord.Embed | None = None,
        artist_url: str | None = None,
        album_url: str | None = None,
        timeout: float = VIEW_TIMEOUT_SECONDS,
    ):
        super().__init__(
            timeout=timeout
        )

        self.username = username
        self.item = dict(
            item
        )

        self.main_embed = main_embed
        self.details_embed = details_embed
        self.tracklist_embed = tracklist_embed

        self._extra_cache = extra

        # Jeśli nie ma Recenzji albo Track Ratings, button w ogóle nie istnieje.
        _set_button_visible(
            self,
            self.review_button,
            _has_review_available(
                self.item,
                extra,
            ),
        )

        _set_button_visible(
            self,
            self.tracks_button,
            _has_track_ratings_available(
                self.item,
                extra,
            ),
        )

        # Extended /last controls only.
        if self.details_embed is None:
            self.remove_item(
                self.details_button
            )

        if self.tracklist_embed is None:
            self.remove_item(
                self.tracklist_button
            )

        if artist_url:
            self.add_item(
                discord.ui.Button(
                    label="Artysta",
                    style=discord.ButtonStyle.link,
                    url=artist_url,
                    row=1,
                )
            )

        if album_url:
            self.add_item(
                discord.ui.Button(
                    label="Album",
                    style=discord.ButtonStyle.link,
                    url=album_url,
                    row=1,
                )
            )

    @discord.ui.button(
        label="Główne",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def main_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=self.main_embed,
            view=self,
        )

    @discord.ui.button(
        label="Szczegóły",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def details_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.details_embed is None:
            await interaction.response.send_message(
                "Brak szczegółów wydania.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=self.details_embed,
            view=self,
        )

    @discord.ui.button(
        label="Tracklista",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def tracklist_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.tracklist_embed is None:
            await interaction.response.send_message(
                "Brak tracklisty.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=self.tracklist_embed,
            view=self,
        )

    @discord.ui.button(
        label="Recenzja",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def review_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer()

        extra = await self._load_extra()

        if extra.get(
            "rate_limited"
        ):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. "
                "Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get(
            "review_text"
        ):
            await interaction.followup.send(
                "Ta ocena nie ma recenzji.",
                ephemeral=True,
            )
            return

        await interaction.message.edit(
            embed=build_review_embed(
                self.username,
                self.item,
                extra,
            ),
            view=self,
        )

    @discord.ui.button(
        label="Track ratings",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def tracks_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer()

        extra = await self._load_extra()

        if extra.get(
            "rate_limited"
        ):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. "
                "Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if (
            extra.get(
                "detail_incomplete"
            )
            and extra.get(
                "has_track_ratings"
            )
            and not extra.get(
                "track_ratings"
            )
        ):
            await interaction.followup.send(
                "⚠️ AOTY potwierdza Track Ratings dla tej oceny, "
                "ale szczegóły nie zostały teraz pobrane. "
                "Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get(
            "has_track_ratings"
        ):
            await interaction.followup.send(
                "Ta ocena nie ma ocen tracklisty.",
                ephemeral=True,
            )
            return

        await interaction.message.edit(
            embed=build_track_ratings_embed(
                self.username,
                self.item,
                extra,
            ),
            view=self,
        )


class RatingSelect(discord.ui.Select):
    def __init__(self, owner, items: list[dict]):
        self.owner = owner
        options = []

        for index, item in enumerate(items):
            artist = display_romanized_name(item.get("artist") or "?")
            album = display_romanized_name(item.get("album") or item.get("title") or "?")
            score = item.get("score") or "NR"
            flags = rating_flags_text(item)
            description = f"{score} {flags}".strip()
            options.append(
                discord.SelectOption(
                    label=f"{artist} — {album}"[:100],
                    value=str(index),
                    description=description[:100] if description else None,
                )
            )

        super().__init__(
            placeholder="Wybierz ocenę dla Recenzji / Track ratings",
            options=options or [discord.SelectOption(label="Brak ocen", value="0")],
            min_values=1,
            max_values=1,
            disabled=not bool(items),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.owner.selected_index = int(self.values[0])
        self.owner._selected_extra = None
        self.owner._refresh_detail_buttons()

        await interaction.response.edit_message(
            view=self.owner,
        )


class MultiRatingView(TimedDisableView):
    """Details controls for a message containing several rating embeds."""

    def __init__(
        self,
        *,
        username: str,
        items: list[dict],
        main_embeds: list[discord.Embed],
        timeout: float = VIEW_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=timeout)
        self.username = username
        self.items = [dict(item) for item in items]
        self.main_embeds = main_embeds
        self.selected_index = 0
        self._selected_extra = None
        self.add_item(
            RatingSelect(
                self,
                self.items,
            )
        )
        self._refresh_detail_buttons()

    def _refresh_detail_buttons(self):
        item = (
            self.items[self.selected_index]
            if self.items
            else {}
        )

        _set_button_visible(
            self,
            self.review_button,
            _has_review_available(
                item,
                self._selected_extra,
            ),
        )

        _set_button_visible(
            self,
            self.tracks_button,
            _has_track_ratings_available(
                item,
                self._selected_extra,
            ),
        )

    async def _extra(self):
        if self._selected_extra is not None:
            return self._selected_extra

        item = self.items[self.selected_index]
        extra = await _load_live_extra(
            self.username,
            item,
            fallback_limit=60,
        )

        if (
            not extra.get("rate_limited")
            and not extra.get("detail_incomplete")
        ):
            self._selected_extra = extra

        return extra

    @discord.ui.button(label="Główne", style=discord.ButtonStyle.secondary, row=0)
    async def main_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embeds=self.main_embeds, view=self)

    @discord.ui.button(label="Recenzja", style=discord.ButtonStyle.secondary, row=0)
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        extra = await self._extra()

        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get("review_text"):
            await interaction.followup.send("Wybrana ocena nie ma recenzji.", ephemeral=True)
            return

        item = self.items[self.selected_index]
        await interaction.message.edit(
            embeds=[build_review_embed(self.username, item, extra)],
            view=self,
        )

    @discord.ui.button(label="Track ratings", style=discord.ButtonStyle.secondary, row=0)
    async def tracks_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        extra = await self._extra()

        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if (
            extra.get("detail_incomplete")
            and extra.get("has_track_ratings")
            and not extra.get("track_ratings")
        ):
            await interaction.followup.send(
                "⚠️ AOTY potwierdza Track Ratings dla tej oceny, "
                "ale szczegóły nie zostały teraz pobrane. "
                "Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get("has_track_ratings"):
            await interaction.followup.send("Wybrana ocena nie ma ocen tracklisty.", ephemeral=True)
            return

        item = self.items[self.selected_index]
        await interaction.message.edit(
            embeds=[build_track_ratings_embed(self.username, item, extra)],
            view=self,
        )


class UserRatingSelect(discord.ui.Select):
    def __init__(self, owner, usernames: list[str], rating_infos: dict[str, dict]):
        self.owner = owner
        options = []

        for username in usernames[:25]:
            info = rating_infos.get(username, {})
            score = info.get("score") or "NR"
            flags = rating_flags_text(info)
            options.append(
                discord.SelectOption(
                    label=username[:100],
                    value=username[:100],
                    description=f"{score} {flags}".strip()[:100],
                )
            )

        super().__init__(
            placeholder="Wybierz użytkownika",
            options=options,
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.owner.selected_username = self.values[0]
        self.owner._refresh_detail_buttons()

        await interaction.response.edit_message(
            view=self.owner,
        )


class AlbumRatingView(TimedDisableView):
    """Review/track detail switcher for /album and config users."""

    def __init__(
        self,
        *,
        main_embed: discord.Embed,
        release_item: dict,
        usernames: list[str],
        rating_infos: dict[str, dict],
        timeout: float = VIEW_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=timeout)
        self.main_embed = main_embed
        self.release_item = dict(release_item)
        self.usernames = usernames
        self.rating_infos = rating_infos
        self.selected_username = usernames[0] if usernames else ""

        if usernames:
            self.add_item(
                UserRatingSelect(
                    self,
                    usernames,
                    rating_infos,
                )
            )

        self._refresh_detail_buttons()

    def _selected_rating_info(self) -> dict:
        if not self.selected_username:
            return {}

        return self.rating_infos.get(
            self.selected_username,
            {},
        )

    def _refresh_detail_buttons(self):
        info = self._selected_rating_info()

        _set_button_visible(
            self,
            self.review_button,
            _has_review_available(
                info,
                info,
            ),
        )

        _set_button_visible(
            self,
            self.tracks_button,
            _has_track_ratings_available(
                info,
                info,
            ),
        )

    async def _extra_for_selected(self) -> dict:
        username = self.selected_username
        cached = self.rating_infos.get(username, {})

        if (
            cached.get("review_text") is not None
            or cached.get("track_ratings")
        ):
            return cached

        selected_item = dict(
            self.release_item
        )

        # Reuse the exact /user/<username>/album/... URL already found during
        # /album's first live lookup. This keeps /album on the same code path
        # as /last /recent /profile and avoids unnecessary ratings-page scans.
        if cached.get("review_url"):
            selected_item["review_url"] = cached.get(
                "review_url"
            )

        if cached.get("score") is not None:
            selected_item["score"] = cached.get(
                "score"
            )

        if cached.get("date"):
            selected_item["date"] = cached.get(
                "date"
            )

        extra = await _load_live_extra(
            username,
            selected_item,
            fallback_limit=60,
        )

        if (
            not extra.get("rate_limited")
            and not extra.get("detail_incomplete")
        ):
            self.rating_infos[username] = extra

        return extra

    @discord.ui.button(label="Główne", style=discord.ButtonStyle.secondary, row=0)
    async def main_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.main_embed, view=self)

    @discord.ui.button(label="Recenzja", style=discord.ButtonStyle.secondary, row=0)
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_username:
            await interaction.response.send_message("Brak użytkowników w configu.", ephemeral=True)
            return

        await interaction.response.defer()
        extra = await self._extra_for_selected()

        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get("review_text"):
            await interaction.followup.send("Ten użytkownik nie ma recenzji tego wydania.", ephemeral=True)
            return

        item = dict(self.release_item)
        item["score"] = extra.get("score")
        item["date"] = extra.get("date")
        await interaction.message.edit(
            embed=build_review_embed(self.selected_username, item, extra),
            view=self,
        )

    @discord.ui.button(label="Track ratings", style=discord.ButtonStyle.secondary, row=0)
    async def tracks_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_username:
            await interaction.response.send_message("Brak użytkowników w configu.", ephemeral=True)
            return

        await interaction.response.defer()
        extra = await self._extra_for_selected()

        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if (
            extra.get("detail_incomplete")
            and extra.get("has_track_ratings")
            and not extra.get("track_ratings")
        ):
            await interaction.followup.send(
                "⚠️ AOTY potwierdza Track Ratings dla tej oceny, "
                "ale szczegóły nie zostały teraz pobrane. "
                "Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get("has_track_ratings"):
            await interaction.followup.send("Ten użytkownik nie ma ocen tracklisty.", ephemeral=True)
            return

        item = dict(self.release_item)
        item["score"] = extra.get("score")
        item["date"] = extra.get("date")
        await interaction.message.edit(
            embed=build_track_ratings_embed(self.selected_username, item, extra),
            view=self,
        )


class ProfileRatingSelect(discord.ui.Select):
    def __init__(self, owner, page_items: list[dict]):
        self.owner = owner
        options = []

        for index, item in enumerate(page_items):
            artist = display_romanized_name(item.get("artist") or "?")
            album = display_romanized_name(item.get("album") or "?")
            options.append(
                discord.SelectOption(
                    label=f"{artist} — {album}"[:100],
                    value=str(index),
                    description=f"{item.get('score') or 'NR'} {rating_flags_text(item)}".strip()[:100],
                )
            )

        super().__init__(
            placeholder="Wybierz ocenę dla Recenzji / Track ratings",
            options=options or [discord.SelectOption(label="Brak ocen", value="0")],
            min_values=1,
            max_values=1,
            disabled=not bool(page_items),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.owner.selected_index = int(self.values[0])
        self.owner._selected_extra = None
        self.owner._rebuild_components()

        await interaction.response.edit_message(
            view=self.owner,
        )


class ProfilePagerView(TimedDisableView):
    """Up to 10 pages of five profile ratings, with dynamic arrows."""

    def __init__(
        self,
        *,
        username: str,
        ratings: list[dict],
        build_page_embed: Callable[[int], discord.Embed],
        timeout: float = VIEW_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=timeout)
        self.username = username
        self.ratings = ratings[:50]
        self.build_page_embed = build_page_embed
        self.page_index = 0
        self.selected_index = 0
        self._selected_extra = None
        self._rebuild_components()

    @property
    def total_pages(self) -> int:
        if not self.ratings:
            return 1
        return min(10, (len(self.ratings) + 4) // 5)

    def page_items(self) -> list[dict]:
        start = self.page_index * 5
        return self.ratings[start:start + 5]

    def _rebuild_components(self):
        self.clear_items()

        if self.page_index > 0:
            previous = discord.ui.Button(label="←", style=discord.ButtonStyle.secondary, row=0)
            previous.callback = self._previous
            self.add_item(previous)

        if self.page_index < self.total_pages - 1:
            next_button = discord.ui.Button(label="→", style=discord.ButtonStyle.secondary, row=0)
            next_button.callback = self._next
            self.add_item(next_button)

        self.add_item(ProfileRatingSelect(self, self.page_items()))

        main = discord.ui.Button(label="Główne", style=discord.ButtonStyle.secondary, row=2)
        main.callback = self._main
        self.add_item(main)

        selected_item = self._selected_item() or {}

        if _has_review_available(
            selected_item,
            self._selected_extra,
        ):
            review = discord.ui.Button(
                label="Recenzja",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            review.callback = self._review
            self.add_item(review)

        if _has_track_ratings_available(
            selected_item,
            self._selected_extra,
        ):
            tracks = discord.ui.Button(
                label="Track ratings",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            tracks.callback = self._tracks
            self.add_item(tracks)

    async def _previous(self, interaction: discord.Interaction):
        if self.page_index <= 0:
            await interaction.response.defer()
            return

        self.page_index -= 1
        self.selected_index = 0
        self._selected_extra = None
        self._rebuild_components()
        await interaction.response.edit_message(
            embed=self.build_page_embed(self.page_index),
            view=self,
        )

    async def _next(self, interaction: discord.Interaction):
        if self.page_index >= self.total_pages - 1:
            await interaction.response.defer()
            return

        self.page_index += 1
        self.selected_index = 0
        self._selected_extra = None
        self._rebuild_components()
        await interaction.response.edit_message(
            embed=self.build_page_embed(self.page_index),
            view=self,
        )

    async def _main(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=self.build_page_embed(self.page_index),
            view=self,
        )

    def _selected_item(self) -> dict | None:
        items = self.page_items()
        if not items:
            return None
        index = min(self.selected_index, len(items) - 1)
        return items[index]

    async def _extra(self, item: dict) -> dict:
        if self._selected_extra is not None:
            return self._selected_extra

        extra = await _load_live_extra(
            self.username,
            item,
            fallback_limit=60,
        )

        if (
            not extra.get("rate_limited")
            and not extra.get("detail_incomplete")
        ):
            self._selected_extra = extra

        return extra

    async def _review(self, interaction: discord.Interaction):
        item = self._selected_item()
        if not item:
            await interaction.response.send_message("Brak oceny na tej stronie.", ephemeral=True)
            return

        await interaction.response.defer()
        extra = await self._extra(item)

        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get("review_text"):
            await interaction.followup.send("Wybrana ocena nie ma recenzji.", ephemeral=True)
            return

        await interaction.message.edit(
            embed=build_review_embed(self.username, item, extra),
            view=self,
        )

    async def _tracks(self, interaction: discord.Interaction):
        item = self._selected_item()
        if not item:
            await interaction.response.send_message("Brak oceny na tej stronie.", ephemeral=True)
            return

        await interaction.response.defer()
        extra = await self._extra(item)

        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if not extra.get("has_track_ratings"):
            await interaction.followup.send("Wybrana ocena nie ma ocen tracklisty.", ephemeral=True)
            return

        await interaction.message.edit(
            embed=build_track_ratings_embed(self.username, item, extra),
            view=self,
        )
