"""Reusable Discord components for reviews, track ratings and profile paging."""

from __future__ import annotations

from collections.abc import Callable

import discord

import aoty
from services import DATA
from display_utils import display_romanized_name
from release_tabs import (
    build_combined_tracklist_embed,
    build_release_details_embed,
    build_review_embed,
)
from settings import USERS
from shared import (
    rating_flags_text,
    score_or_nr,
)


HOME_BUTTON = "🏠︎"
TRACKLIST_BUTTON = "☰"
REVIEW_BUTTON = "✎"
DETAILS_BUTTON = "🛈"
ARTIST_BUTTON = "★"
# One declarative source for every interactive command.  Individual views only
# provide callbacks and availability; changing order or symbols happens here.
ACTION_TABS = {
    "artist": ARTIST_BUTTON,
    "details": DETAILS_BUTTON,
    "home": HOME_BUTTON,
    "tracklist": TRACKLIST_BUTTON,
    "review": REVIEW_BUTTON,
}
ACTION_BUTTON_ORDER = tuple(ACTION_TABS.values())


async def _aoty_avatar(username: str) -> str | None:
    """Read an already-cached AOTY avatar without making a web request."""

    try:
        return await DATA.get_avatar(username)
    except Exception:
        return None


def _release_action_available(item: dict | None) -> bool:
    item = item or {}
    return bool(
        (item.get("album") or item.get("title"))
        and (item.get("album_id") or item.get("url"))
    )


def _tracklist_available(item: dict | None) -> bool:
    item = item or {}
    if item.get("tracklist") or item.get("_has_tracklist"):
        return True

    album_id = str(
        item.get("album_id")
        or aoty.extract_album_id(item.get("url"))
        or ""
    )
    if not album_id:
        return False

    cached_release = DATA.cached_release_details(album_id)
    if cached_release and cached_release.get("tracklist"):
        return True

    # A personal Track Rating row can reconstruct the tracklist even when the
    # public release cache is not complete yet.
    for username in USERS[:25]:
        cached_rating = DATA.cached_rating(username, album_id)
        if cached_rating and cached_rating.get("track_ratings"):
            return True
    return False


def _artist_name(item: dict | None) -> str:
    item = item or {}
    if item.get("_position_kind") == "favorite_artist":
        return str(item.get("name") or item.get("artist") or "").strip()
    return str(item.get("artist") or "").strip()


def _artist_action_available(item: dict | None) -> bool:
    return bool(_artist_name(item))


async def _show_artist_command(
    interaction: discord.Interaction,
    item: dict,
    *,
    source_view: discord.ui.View | None = None,
) -> None:
    artist = _artist_name(item)
    if not artist:
        await interaction.response.send_message(
            "Brak artysty dla tej pozycji.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    try:
        # Runtime import avoids a module cycle: commands.artist imports the
        # common TimedDisableView from this module.
        from commands.artist import build_artist_response

        result = await build_artist_response(
            artist,
            owner_id=getattr(source_view, "owner_id", None),
        )
        if result is None:
            await interaction.followup.send(
                f"❌ Nie znaleziono artysty **{artist}** na AOTY ani w SQLite.",
                ephemeral=False,
            )
            return
        embed, artist_view = result
        if source_view is not None:
            await _clear_artist_result(source_view)
            # The main command retains its five global actions. /artist gets
            # its own second message, so its sort buttons and dropdowns can
            # remain exactly the same as in the standalone command.
            await _edit_component_message(interaction,
                view=source_view,
            )
            message = await interaction.followup.send(
                embed=embed,
                view=artist_view,
                ephemeral=False,
                wait=True,
            )
            source_view.artist_message = message
            artist_view.bind_message(message)
            return

        message = await interaction.followup.send(
            embed=embed,
            view=artist_view,
            ephemeral=False,
            wait=True,
        )
        artist_view.bind_message(message)
    except aoty.AOTYRateLimit:
        await interaction.followup.send(
            "⚠️ AOTY chwilowo ogranicza liczbę zapytań.",
            ephemeral=False,
        )
    except Exception as exc:
        await interaction.followup.send(
            f"❌ Nie udało się otworzyć artysty: `{type(exc).__name__}: {exc}`",
            ephemeral=False,
        )


async def _clear_artist_result(view: discord.ui.View) -> None:
    message = getattr(view, "artist_message", None)
    if message is None:
        return
    view.artist_message = None
    try:
        await message.delete()
    except Exception:
        pass


async def _edit_component_message(
    interaction: discord.Interaction,
    **kwargs,
) -> None:
    """Edit the message that owns a component after a deferred callback.

    ``interaction.message`` is usually a webhook-backed message.  On some
    clients it can be stale after ``defer`` and silently leave Home visible.
    The interaction's original response is the component's owning message,
    so it is the authoritative route.  The direct-message fallback keeps
    manually created messages and test doubles compatible.
    """

    try:
        await interaction.edit_original_response(**kwargs)
    except (AttributeError, discord.HTTPException):
        await interaction.message.edit(**kwargs)


async def _open_release_tab(
    interaction: discord.Interaction,
    source_view: discord.ui.View,
    embed: discord.Embed,
    *,
    tab_view: discord.ui.View | None = None,
) -> None:
    """Keep Home intact and publish one lower, disposable tab message."""

    await _edit_component_message(interaction, view=source_view)
    message = await interaction.followup.send(
        embed=embed,
        view=tab_view,
        ephemeral=False,
        wait=True,
    )
    source_view.artist_message = message
    bind_message = getattr(tab_view, "bind_message", None)
    if callable(bind_message):
        bind_message(message)


VIEW_TIMEOUT_SECONDS = 20 * 60


class TimedDisableView(discord.ui.View):
    """A view that visibly disables every control after 20 minutes."""

    def __init__(
        self,
        *,
        timeout: float = VIEW_TIMEOUT_SECONDS,
        owner_id: int | None = None,
    ):
        super().__init__(timeout=timeout)
        # Components represent one command session.  Public embeds remain
        # visible to everyone, but changing a select/button is reserved for
        # the person who opened that session.
        self.owner_id = int(owner_id) if owner_id is not None else None
        self.message = None
        self.artist_message = None
        self._bound_channel = None
        self._bound_message_id: int | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Allow only the original invoker to use this public view."""

        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if self.owner_id is None or user_id == self.owner_id:
            return True

        await interaction.response.send_message(
            "Tylko osoba, która wywołała tę komendę, może używać tych kontrolek.",
            ephemeral=True,
        )
        return False

    def bind_message(self, message) -> None:
        """Remember a public message in a form that survives webhook expiry.

        Interaction follow-up tokens expire after roughly fifteen minutes,
        while Kotone intentionally leaves controls usable for twenty. Editing
        a ``WebhookMessage`` directly at timeout therefore fails silently.
        Keeping the channel + message ID lets us edit the normal Discord
        message with the bot token instead.
        """
        self.message = message
        self._bound_channel = getattr(message, "channel", None)
        try:
            self._bound_message_id = int(getattr(message, "id", 0) or 0) or None
        except (TypeError, ValueError):
            self._bound_message_id = None

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

        if self.message is None:
            return

        # Prefer a regular channel message over the original webhook object:
        # the latter can no longer authenticate after the interaction token
        # expires. This path works for the public command messages Kotone
        # creates; the fallback keeps existing test/dummy messages compatible.
        channel = self._bound_channel
        message_id = self._bound_message_id
        get_partial = getattr(channel, "get_partial_message", None)
        if channel is not None and message_id and callable(get_partial):
            try:
                await get_partial(message_id).edit(view=self)
                return
            except Exception:
                pass

        try:
            await self.message.edit(view=self)
        except Exception:
            # Deleted message / missing permission: controls are still dead
            # server-side because the View itself has timed out.
            pass


async def _load_live_extra(
    username: str,
    item: dict,
    *,
    fallback_limit: int | None = 60,
) -> dict:
    """Load review/track ratings through SQLite-first shared service.

    Config users reuse persisted detail and only touch AOTY when it is missing
    or stale. Other users remain live-only.
    """
    try:
        return await DATA.get_user_rating_for_album(
            username,
            item.get("album_id"),
            item.get("url"),
            item.get("release_format") or item.get("album_format"),
            fallback_limit=fallback_limit,
            user_release_url=item.get("review_url"),
            album_title=item.get("album") or item.get("title"),
            require_detail=True,
            allow_network=False,
        )
    except aoty.AOTYRateLimit as exc:
        return {
            "score": item.get("score"),
            "date": item.get("date"),
            "review_url": item.get("review_url"),
            "review_text": item.get("review_text"),
            "has_review": bool(item.get("has_review")),
            "track_ratings": list(item.get("track_ratings") or []),
            "has_track_ratings": bool(item.get("has_track_ratings")),
            "liked": bool(item.get("liked")),
            "rate_limited": True,
            "rate_limit_error": str(exc),
        }
    except Exception as exc:
        # A button should never produce discord.ui's noisy unhandled traceback.
        return {
            "score": item.get("score"),
            "date": item.get("date"),
            "review_url": item.get("review_url"),
            "review_text": item.get("review_text"),
            "has_review": bool(item.get("has_review")),
            "track_ratings": list(item.get("track_ratings") or []),
            "has_track_ratings": bool(item.get("has_track_ratings")),
            "liked": bool(item.get("liked")),
            "detail_incomplete": True,
            "load_error": f"{type(exc).__name__}: {exc}",
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


def _review_detail_temporarily_unavailable(extra: dict | None) -> bool:
    """A card confirms a review, but its body could not be fetched safely."""

    extra = extra or {}
    return bool(
        extra.get("detail_incomplete")
        and extra.get("has_review")
        and not extra.get("review_text")
    )


async def _send_review_unavailable(interaction: discord.Interaction) -> None:
    await interaction.followup.send(
        "⚠️ AOTY potwierdza recenzję dla tej oceny, "
        "ale jej treść nie została teraz pobrana. "
        "Spróbuj ponownie za chwilę.",
        ephemeral=True,
    )


def _set_button_enabled(
    button: discord.ui.Item,
    enabled: bool,
) -> None:
    """Keep an applicable action visible and disable it when unavailable."""
    button.disabled = not bool(enabled)


def _set_active_action(
    view: discord.ui.View,
    active_label: str,
) -> None:
    for child in view.children:
        if (
            isinstance(child, discord.ui.Button)
            and child.label in ACTION_BUTTON_ORDER
        ):
            # Home is the stable blue anchor. The currently open auxiliary
            # tab is green, which makes the selected lower embed unambiguous.
            child.style = (
                discord.ButtonStyle.primary
                if child.label == HOME_BUTTON
                else (
                    discord.ButtonStyle.success
                    if child.label == active_label
                    else discord.ButtonStyle.secondary
                )
            )


def _order_action_buttons(
    view: discord.ui.View,
    *buttons: discord.ui.Button,
) -> None:
    """Apply the one canonical action order inside a Discord component row."""
    by_label = {button.label: button for button in buttons}
    buttons = tuple(
        by_label[label]
        for label in ACTION_BUTTON_ORDER
        if label in by_label
    )
    for button in buttons:
        if button in view.children:
            view.remove_item(button)
    for button in buttons:
        view.add_item(button)



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
    """Shared five-action switcher for one rating."""

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
        author_icon_url: str | None = None,
        timeout: float = VIEW_TIMEOUT_SECONDS,
        owner_id: int | None = None,
    ):
        super().__init__(
            timeout=timeout,
            owner_id=owner_id,
        )

        self.username = username
        self.item = dict(
            item
        )

        self.main_embed = main_embed
        self.details_embed = details_embed
        self.tracklist_embed = tracklist_embed
        self._extra_cache = extra
        self.author_icon_url = author_icon_url

        # Keep compatibility with existing command arguments while making the
        # selected item self-contained for all shared callbacks.
        if artist_url:
            self.item.setdefault("artist_url", artist_url)
        if album_url:
            self.item.setdefault("url", album_url)

        _set_button_enabled(self.artist_button, _artist_action_available(self.item))
        _set_button_enabled(self.main_button, True)
        _set_button_enabled(
            self.tracklist_button,
            _tracklist_available(self.item),
        )
        _set_button_enabled(
            self.review_button,
            _has_review_available(self.item, extra),
        )
        _set_button_enabled(
            self.details_button,
            _release_action_available(self.item) or self.details_embed is not None,
        )
        _order_action_buttons(
            self,
            self.artist_button,
            self.details_button,
            self.main_button,
            self.tracklist_button,
            self.review_button,
        )
        _set_active_action(self, HOME_BUTTON)

    @discord.ui.button(
        label=ARTIST_BUTTON,
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def artist_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        _set_active_action(self, ARTIST_BUTTON)
        await _show_artist_command(
            interaction,
            self.item,
            source_view=self,
        )

    @discord.ui.button(
        label=HOME_BUTTON,
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def main_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        _set_active_action(self, HOME_BUTTON)
        await _clear_artist_result(self)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label=TRACKLIST_BUTTON,
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def tracklist_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        _set_active_action(self, TRACKLIST_BUTTON)
        await interaction.response.defer()
        await _clear_artist_result(self)
        embed = await build_combined_tracklist_embed(
            self.item,
            username=self.username,
            author_icon_url=self.author_icon_url,
        )
        await _open_release_tab(interaction, self, embed)

    @discord.ui.button(
        label=REVIEW_BUTTON,
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def review_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer()
        await _clear_artist_result(self)

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

        if _review_detail_temporarily_unavailable(extra):
            await _send_review_unavailable(interaction)
            return

        if not extra.get(
            "review_text"
        ):
            await interaction.followup.send(
                "Ta ocena nie ma recenzji.",
                ephemeral=True,
            )
            return

        _set_active_action(self, REVIEW_BUTTON)
        await _open_release_tab(
            interaction,
            self,
            build_review_embed(
                self.username,
                self.item,
                extra,
                author_icon_url=self.author_icon_url,
            ),
        )

    @discord.ui.button(
        label=DETAILS_BUTTON,
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def details_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        _set_active_action(self, DETAILS_BUTTON)
        if self.details_embed is not None:
            await interaction.response.defer()
            await _clear_artist_result(self)
            await _open_release_tab(interaction, self, self.details_embed)
            return

        await interaction.response.defer()
        await _clear_artist_result(self)
        embed = await build_release_details_embed(
            self.item,
            username=self.username,
            author_icon_url=self.author_icon_url,
        )
        await _open_release_tab(interaction, self, embed)


class RatingSelect(discord.ui.Select):
    def __init__(self, owner, items: list[dict]):
        self.owner = owner
        options = []

        for index, item in enumerate(items):
            artist = display_romanized_name(item.get("artist") or "—")
            album = display_romanized_name(item.get("album") or item.get("title") or "—")
            score = score_or_nr(item.get("score"))
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
            placeholder="Wybierz pozycję",
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
        await _clear_artist_result(self.owner)


class MultiRatingView(TimedDisableView):
    """Details controls for a message containing several rating embeds."""

    def __init__(
        self,
        *,
        username: str,
        items: list[dict],
        main_embeds: list[discord.Embed],
        author_icon_url: str | None = None,
        timeout: float = VIEW_TIMEOUT_SECONDS,
        owner_id: int | None = None,
    ):
        super().__init__(timeout=timeout, owner_id=owner_id)
        self.username = username
        self.items = [dict(item) for item in items]
        self.main_embeds = main_embeds
        self.selected_index = 0
        self._selected_extra = None
        self.author_icon_url = author_icon_url
        self.add_item(
            RatingSelect(
                self,
                self.items,
            )
        )
        self._refresh_detail_buttons()
        _order_action_buttons(
            self,
            self.artist_button,
            self.details_button,
            self.main_button,
            self.tracklist_button,
            self.review_button,
        )
        _set_active_action(self, HOME_BUTTON)

    def _refresh_detail_buttons(self):
        item = (
            self.items[self.selected_index]
            if self.items
            else {}
        )
        _set_button_enabled(self.artist_button, _artist_action_available(item))
        _set_button_enabled(self.main_button, bool(self.main_embeds))
        _set_button_enabled(self.tracklist_button, _tracklist_available(item))
        _set_button_enabled(
            self.review_button,
            _has_review_available(item, self._selected_extra),
        )
        _set_button_enabled(self.details_button, _release_action_available(item))

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

    @discord.ui.button(label=ARTIST_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def artist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = self.items[self.selected_index] if self.items else {}
        _set_active_action(self, ARTIST_BUTTON)
        await _show_artist_command(interaction, item, source_view=self)

    @discord.ui.button(label=HOME_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def main_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _set_active_action(self, HOME_BUTTON)
        await _clear_artist_result(self)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label=TRACKLIST_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def tracklist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = self.items[self.selected_index] if self.items else {}
        _set_active_action(self, TRACKLIST_BUTTON)
        await interaction.response.defer()
        await _clear_artist_result(self)
        embed = await build_combined_tracklist_embed(
            item,
            username=self.username,
            author_icon_url=self.author_icon_url,
        )
        await _open_release_tab(interaction, self, embed)

    @discord.ui.button(label=REVIEW_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _clear_artist_result(self)
        extra = await self._extra()

        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return

        if _review_detail_temporarily_unavailable(extra):
            await _send_review_unavailable(interaction)
            return

        if not extra.get("review_text"):
            await interaction.followup.send("Wybrana ocena nie ma recenzji.", ephemeral=True)
            return

        item = self.items[self.selected_index]
        _set_active_action(self, REVIEW_BUTTON)
        await _open_release_tab(
            interaction,
            self,
            build_review_embed(
                self.username,
                item,
                extra,
                author_icon_url=self.author_icon_url,
            ),
        )

    @discord.ui.button(label=DETAILS_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def details_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = self.items[self.selected_index] if self.items else {}
        _set_active_action(self, DETAILS_BUTTON)
        await interaction.response.defer()
        await _clear_artist_result(self)
        embed = await build_release_details_embed(
            item,
            username=self.username,
            author_icon_url=self.author_icon_url,
        )
        await _open_release_tab(interaction, self, embed)


class UserRatingSelect(discord.ui.Select):
    def __init__(
        self,
        owner,
        usernames: list[str],
        rating_infos: dict[str, dict],
        *,
        on_change=None,
    ):
        self.owner = owner
        self.on_change = on_change
        options = []

        for username in usernames[:25]:
            info = rating_infos.get(username, {})
            score = score_or_nr(info.get("score"))
            flags = rating_flags_text(info)
            options.append(
                discord.SelectOption(
                    label=username[:100],
                    value=username[:100],
                    description=f"{score} {flags}".strip()[:100],
                    default=username == owner.selected_username,
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
        for option in self.options:
            option.default = option.value == self.owner.selected_username
        self.owner._refresh_detail_buttons()
        if self.on_change is not None:
            await self.on_change(interaction)
            return
        await self.owner._show_selected_review(interaction)


class AlbumReviewTabView(TimedDisableView):
    """The review-only user picker shown beneath the lower review embed."""

    def __init__(self, owner: "AlbumRatingView"):
        super().__init__(owner_id=owner.owner_id)
        self.owner = owner
        self.add_item(
            UserRatingSelect(
                owner,
                owner.usernames,
                owner.rating_infos,
                on_change=self._change_user,
            )
        )

    async def _change_user(self, interaction: discord.Interaction) -> None:
        await self.owner._show_selected_review(interaction, tab_view=self)


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
        owner_id: int | None = None,
    ):
        super().__init__(timeout=timeout, owner_id=owner_id)
        self.main_embed = main_embed
        self.release_item = dict(release_item)
        self.usernames = usernames
        self.rating_infos = rating_infos
        self.selected_username = next(
            (
                username
                for username in usernames
                if _has_review_available(
                    rating_infos.get(username, {}),
                    rating_infos.get(username, {}),
                )
            ),
            usernames[0] if usernames else "",
        )
        self._refresh_detail_buttons()
        _order_action_buttons(
            self,
            self.artist_button,
            self.details_button,
            self.main_button,
            self.tracklist_button,
            self.review_button,
        )
        _set_active_action(self, HOME_BUTTON)

    def _selected_rating_info(self) -> dict:
        if not self.selected_username:
            return {}

        return self.rating_infos.get(
            self.selected_username,
            {},
        )

    def _refresh_detail_buttons(self):
        _set_button_enabled(
            self.artist_button,
            _artist_action_available(self.release_item),
        )
        _set_button_enabled(self.main_button, True)
        _set_button_enabled(
            self.tracklist_button,
            _tracklist_available(self.release_item),
        )
        _set_button_enabled(
            self.review_button,
            any(
                _has_review_available(
                    self.rating_infos.get(username, {}),
                    self.rating_infos.get(username, {}),
                )
                for username in self.usernames
            ),
        )
        _set_button_enabled(
            self.details_button,
            _release_action_available(self.release_item),
        )

    async def _extra_for_selected(self) -> dict:
        username = self.selected_username
        cached = self.rating_infos.get(username, {})

        # /album initially asks for a compact rating card. A DB card can have
        # review_text/detail_complete but deliberately does not join the
        # user_track_ratings rows. Only a payload that explicitly contains the
        # track list is complete enough to serve both detail buttons.
        if (
            "track_ratings" in cached
            and not cached.get("detail_incomplete")
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

        # Preserve card-level evidence in the error fallback. Without these
        # flags a failed detail refresh would turn a known review/Track Ratings
        # into the misleading normal "brak" response.
        for key in (
            "has_review",
            "has_track_ratings",
            "liked",
            "review_text",
        ):
            if key in cached:
                selected_item[key] = cached.get(key)

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

    async def _show_selected_review(
        self,
        interaction: discord.Interaction,
        *,
        tab_view: AlbumReviewTabView | None = None,
    ) -> None:
        if not self.selected_username:
            await interaction.response.send_message(
                "Brak użytkowników w configu.",
                ephemeral=True,
            )
            return

        cached = self._selected_rating_info()
        if not _has_review_available(cached, cached):
            await interaction.response.send_message(
                "Ten użytkownik nie ma recenzji tego wydania.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        if tab_view is None:
            await _clear_artist_result(self)
        extra = await self._extra_for_selected()
        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return
        if _review_detail_temporarily_unavailable(extra):
            await _send_review_unavailable(interaction)
            return
        if not extra.get("review_text"):
            await interaction.followup.send(
                "Ten użytkownik nie ma recenzji tego wydania.",
                ephemeral=True,
            )
            return

        item = dict(self.release_item)
        item["score"] = extra.get("score")
        item["date"] = extra.get("date")
        avatar = await _aoty_avatar(self.selected_username)
        review_embed = build_review_embed(
            self.selected_username,
            item,
            extra,
            author_icon_url=avatar,
        )
        if tab_view is not None:
            await _edit_component_message(
                interaction,
                embed=review_embed,
                view=tab_view,
            )
            return

        _set_active_action(self, REVIEW_BUTTON)
        await _open_release_tab(
            interaction,
            self,
            review_embed,
            tab_view=AlbumReviewTabView(self),
        )

    @discord.ui.button(label=ARTIST_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def artist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _set_active_action(self, ARTIST_BUTTON)
        await _show_artist_command(
            interaction,
            self.release_item,
            source_view=self,
        )

    @discord.ui.button(label=HOME_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def main_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _set_active_action(self, HOME_BUTTON)
        await _clear_artist_result(self)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label=TRACKLIST_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def tracklist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _set_active_action(self, TRACKLIST_BUTTON)
        await interaction.response.defer()
        await _clear_artist_result(self)
        embed = await build_combined_tracklist_embed(self.release_item)
        await _open_release_tab(interaction, self, embed)

    @discord.ui.button(label=REVIEW_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_selected_review(interaction)

    @discord.ui.button(label=DETAILS_BUTTON, style=discord.ButtonStyle.secondary, row=0)
    async def details_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _set_active_action(self, DETAILS_BUTTON)
        await interaction.response.defer()
        await _clear_artist_result(self)
        embed = await build_release_details_embed(self.release_item)
        await _open_release_tab(interaction, self, embed)


def _normalize_profile_favorite(item: dict) -> dict:
    normalized = dict(item or {})
    item_type = str(
        normalized.get("type") or normalized.get("item_type") or "album"
    ).casefold()
    if item_type == "artist":
        name = normalized.get("name") or normalized.get("artist")
        normalized.update(
            {
                "_position_kind": "favorite_artist",
                "name": name,
                "artist": name,
                "artist_url": normalized.get("url"),
            }
        )
        return normalized

    normalized["_position_kind"] = "favorite_album"
    normalized.setdefault("album", normalized.get("name"))
    album_id = normalized.get("album_id") or aoty.extract_album_id(
        normalized.get("url")
    )
    if album_id:
        normalized["album_id"] = str(album_id)
    return normalized


class ProfilePositionSelect(discord.ui.Select):
    def __init__(self, owner):
        self.owner = owner
        options: list[discord.SelectOption] = []
        page_start = owner.page_index * 5

        for offset, item in enumerate(owner.page_items()):
            absolute_index = page_start + offset
            artist = display_romanized_name(item.get("artist") or "—")
            album = display_romanized_name(item.get("album") or "—")
            options.append(
                discord.SelectOption(
                    label=f"{artist} — {album}"[:100],
                    value=f"rating:{absolute_index}",
                    description=(
                        f"Ocena • {score_or_nr(item.get('score'))} "
                        f"{rating_flags_text(item)}"
                    ).strip()[:100],
                    default=(
                        owner.selected_source == "rating"
                        and owner.selected_index == absolute_index
                    ),
                )
            )

        # Five ratings + up to twenty favorites fit Discord's 25-option limit.
        for favorite_index, item in enumerate(owner.favorites[:20]):
            is_artist = item.get("_position_kind") == "favorite_artist"
            if is_artist:
                label = display_romanized_name(item.get("name") or "Nieznany artysta")
                description = "Favorite Artist"
            else:
                artist = display_romanized_name(item.get("artist") or "—")
                album = display_romanized_name(item.get("album") or "Nieznane wydanie")
                label = f"{artist} — {album}" if item.get("artist") else album
                description = "Favorite Album"
            options.append(
                discord.SelectOption(
                    label=f"⭐ {label}"[:100],
                    value=f"favorite:{favorite_index}",
                    description=description,
                    default=(
                        owner.selected_source == "favorite"
                        and owner.selected_index == favorite_index
                    ),
                )
            )

        super().__init__(
            placeholder="Wybierz pozycję",
            options=options or [discord.SelectOption(label="Brak pozycji", value="none")],
            min_values=1,
            max_values=1,
            disabled=not bool(options),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        source, _, raw_index = self.values[0].partition(":")
        if source not in {"rating", "favorite"} or not raw_index.isdigit():
            await interaction.response.defer()
            return
        self.owner.selected_source = source
        self.owner.selected_index = int(raw_index)
        self.owner._selected_extra = None
        self.owner._rebuild_components()
        await interaction.response.edit_message(view=self.owner)
        await _clear_artist_result(self.owner)


class ProfilePagerView(TimedDisableView):
    """Profile paging plus shared actions for ratings and favorites."""

    def __init__(
        self,
        *,
        username: str,
        ratings: list[dict],
        build_page_embed: Callable[[int], discord.Embed],
        favorites: list[dict] | None = None,
        timeout: float = VIEW_TIMEOUT_SECONDS,
        owner_id: int | None = None,
    ):
        super().__init__(timeout=timeout, owner_id=owner_id)
        self.username = username
        self.ratings = [dict(item) for item in ratings[:50]]
        self.favorites = [
            _normalize_profile_favorite(item)
            for item in (favorites or [])
        ]
        self.build_page_embed = build_page_embed
        self.page_index = 0
        self.current_tab = HOME_BUTTON
        self.selected_source = "rating" if self.ratings else "favorite"
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

    def _selected_item(self) -> dict | None:
        if self.selected_source == "favorite":
            if not self.favorites:
                return None
            return self.favorites[min(self.selected_index, len(self.favorites) - 1)]
        if not self.ratings:
            return None
        return self.ratings[min(self.selected_index, len(self.ratings) - 1)]

    def _rebuild_components(self):
        self.clear_items()

        if self.current_tab == HOME_BUTTON:
            previous = discord.ui.Button(
                label="←",
                style=discord.ButtonStyle.secondary,
                row=0,
                disabled=self.page_index <= 0,
            )
            previous.callback = self._previous
            self.add_item(previous)
            next_button = discord.ui.Button(
                label="→",
                style=discord.ButtonStyle.secondary,
                row=0,
                disabled=self.page_index >= self.total_pages - 1,
            )
            next_button.callback = self._next
            self.add_item(next_button)

        self.add_item(ProfilePositionSelect(self))
        selected_item = self._selected_item() or {}
        is_rating = self.selected_source == "rating" and bool(selected_item)

        actions = [
            (ARTIST_BUTTON, self._artist, _artist_action_available(selected_item)),
            (DETAILS_BUTTON, self._details, _release_action_available(selected_item)),
            (HOME_BUTTON, self._main, True),
            (TRACKLIST_BUTTON, self._tracklist, _tracklist_available(selected_item)),
            (
                REVIEW_BUTTON,
                self._review,
                is_rating and _has_review_available(selected_item, self._selected_extra),
            ),
        ]
        for label, callback, enabled in actions:
            button = discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.primary
                    if label == HOME_BUTTON
                    else (
                        discord.ButtonStyle.success
                        if label == self.current_tab
                        else discord.ButtonStyle.secondary
                    )
                ),
                row=2,
                disabled=not enabled,
            )
            button.callback = callback
            self.add_item(button)

    async def _previous(self, interaction: discord.Interaction):
        if self.page_index <= 0:
            await interaction.response.defer()
            return
        self.page_index -= 1
        self.selected_source = "rating"
        self.selected_index = self.page_index * 5
        self._selected_extra = None
        self._rebuild_components()
        await interaction.response.edit_message(
            embeds=[self.build_page_embed(self.page_index)],
            view=self,
        )
        await _clear_artist_result(self)

    async def _next(self, interaction: discord.Interaction):
        if self.page_index >= self.total_pages - 1:
            await interaction.response.defer()
            return
        self.page_index += 1
        self.selected_source = "rating"
        self.selected_index = self.page_index * 5
        self._selected_extra = None
        self._rebuild_components()
        await interaction.response.edit_message(
            embeds=[self.build_page_embed(self.page_index)],
            view=self,
        )
        await _clear_artist_result(self)

    async def _main(self, interaction: discord.Interaction):
        self.current_tab = HOME_BUTTON
        self._rebuild_components()
        await interaction.response.edit_message(
            embeds=[self.build_page_embed(self.page_index)],
            view=self,
        )
        await _clear_artist_result(self)

    async def _extra(self, item: dict) -> dict:
        if self._selected_extra is not None:
            return self._selected_extra
        extra = await _load_live_extra(self.username, item, fallback_limit=60)
        if not extra.get("rate_limited") and not extra.get("detail_incomplete"):
            self._selected_extra = extra
        return extra

    async def _artist(self, interaction: discord.Interaction):
        self.current_tab = ARTIST_BUTTON
        self._rebuild_components()
        await _show_artist_command(
            interaction,
            self._selected_item() or {},
            source_view=self,
        )

    async def _tracklist(self, interaction: discord.Interaction):
        item = self._selected_item()
        if not item:
            await interaction.response.send_message("Brak wybranej pozycji.", ephemeral=True)
            return
        self.current_tab = TRACKLIST_BUTTON
        self._rebuild_components()
        await interaction.response.defer()
        await _clear_artist_result(self)
        avatar = await _aoty_avatar(self.username)
        embed = await build_combined_tracklist_embed(
            item,
            username=self.username,
            author_icon_url=avatar,
        )
        await _open_release_tab(interaction, self, embed)

    # Compatibility name for older tests/callers; this is intentionally the
    # same combined public + configured-user tracklist now.
    async def _tracks(self, interaction: discord.Interaction):
        await self._tracklist(interaction)

    async def _review(self, interaction: discord.Interaction):
        item = self._selected_item()
        if not item or self.selected_source != "rating":
            await interaction.response.send_message("Brak recenzji dla tej pozycji.", ephemeral=True)
            return
        await interaction.response.defer()
        await _clear_artist_result(self)
        extra = await self._extra(item)
        if extra.get("rate_limited"):
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return
        if _review_detail_temporarily_unavailable(extra):
            await _send_review_unavailable(interaction)
            return
        if not extra.get("review_text"):
            await interaction.followup.send("Wybrana pozycja nie ma recenzji.", ephemeral=True)
            return
        self.current_tab = REVIEW_BUTTON
        self._rebuild_components()
        avatar = await _aoty_avatar(self.username)
        await _open_release_tab(
            interaction,
            self,
            build_review_embed(
                self.username,
                item,
                extra,
                author_icon_url=avatar,
            ),
        )

    async def _details(self, interaction: discord.Interaction):
        item = self._selected_item()
        if not item:
            await interaction.response.send_message("Brak wybranej pozycji.", ephemeral=True)
            return
        self.current_tab = DETAILS_BUTTON
        self._rebuild_components()
        await interaction.response.defer()
        await _clear_artist_result(self)
        embed = await build_release_details_embed(
            item,
            username=self.username if self.selected_source == "rating" else None,
            author_icon_url=(
                await _aoty_avatar(self.username)
                if self.selected_source == "rating"
                else None
            ),
        )
        await _open_release_tab(interaction, self, embed)
