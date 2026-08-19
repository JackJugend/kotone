import discord
import requests

import aoty
import musicbrainz
from commands.album import _music_from_presence
from services import DATA
from lastfm_database import LASTFM_DB
from display_utils import display_genres, display_romanized_name
from formats import RATING_FORMATS, format_key_from_label
from presence_cache import PRESENCE_CACHE
from settings import KOTONE_USERS_BY_DISCORD_ID
from shared import (
    aoty_score_or_missing,
    build_release_variables,
    must_hear_title_marker,
    score_or_missing,
    set_aoty_footer,
)
from views import TimedDisableView, VIEW_TIMEOUT_SECONDS


MAX_ARTIST_RELEASES = 18
ARTIST_VIEW_TIMEOUT_SECONDS = VIEW_TIMEOUT_SECONDS


SORT_LABELS = {
    "newest": "Najnowsze",
    "oldest": "Najstarsze",
    "score_desc": "Ocena ↓",
    "score_asc": "Ocena ↑",
    "title_asc": "A–Z",
    "title_desc": "Z–A",
}


def _score_number(value):
    """Convert a whole/decimal AOTY score; NR/None always sort last."""
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _prepare_release_order(releases):
    """Remember AOTY's original discography order."""
    prepared = []

    for index, release in enumerate(releases):
        item = dict(release)
        item["_artist_original_index"] = index
        prepared.append(item)

    return prepared


def _sort_releases(releases, sort_key):
    releases = list(releases)

    if sort_key == "oldest":
        return sorted(
            releases,
            key=lambda item: item.get("_artist_original_index", 0),
            reverse=True,
        )

    if sort_key == "score_desc":
        return sorted(
            releases,
            key=lambda item: (
                _score_number(item.get("user_score")) is None,
                -(
                    _score_number(item.get("user_score"))
                    if _score_number(item.get("user_score")) is not None
                    else -1
                ),
                item.get("_artist_original_index", 0),
            ),
        )

    if sort_key == "score_asc":
        return sorted(
            releases,
            key=lambda item: (
                _score_number(item.get("user_score")) is None,
                (
                    _score_number(item.get("user_score"))
                    if _score_number(item.get("user_score")) is not None
                    else 101
                ),
                item.get("_artist_original_index", 0),
            ),
        )

    if sort_key == "title_asc":
        return sorted(
            releases,
            key=lambda item: (
                display_romanized_name(
                    item.get("title")
                    or item.get("album")
                    or ""
                ).casefold(),
                item.get("_artist_original_index", 0),
            ),
        )

    if sort_key == "title_desc":
        return sorted(
            releases,
            key=lambda item: (
                display_romanized_name(
                    item.get("title")
                    or item.get("album")
                    or ""
                ).casefold(),
                item.get("_artist_original_index", 0),
            ),
            reverse=True,
        )

    # Domyślnie zachowujemy kolejność AOTY: najnowsze -> najstarsze.
    return sorted(
        releases,
        key=lambda item: item.get("_artist_original_index", 0),
    )


def _release_year(release):
    """Return YYYY only when AOTY actually supplied a usable year."""
    year = release.get("year")

    if year is None:
        return None

    year = str(year).strip()

    if len(year) == 4 and year.isdigit():
        return year

    return None


def _format_key_for_release(release):
    """Przetłumacz etykietę wydania przez centralny katalog formatów."""

    return format_key_from_label(release.get("album_format"))


def _artist_relation_text(items):
    """Markdown links for Members / Member Of."""
    parts = []

    for item in items or []:
        name = display_romanized_name(
            item.get("name") or ""
        )

        url = item.get(
            "url"
        )

        if not name:
            continue

        if url:
            parts.append(
                f"[{name}]({url})"
            )
        else:
            parts.append(
                name
            )

    return ", ".join(
        parts
    )


def _country_flag(country_code) -> str:
    """Convert a two-letter MusicBrainz country code to a flag emoji."""

    code = str(country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(127397 + ord(character)) for character in code)


def _artist_header_text(discography):
    """Metadata block displayed above the releases in /artist."""
    if discography.get("source") == "kotone db":
        release_count = len(discography.get("releases") or [])
        lines = [
            f"\💾 **Baza danych Kotone: {release_count} zapisanych releases.**"
        ]
        genres_text = ", ".join(display_genres(discography.get("genres") or []))
        genres_text = genres_text or discography.get("genres_text")
        if genres_text:
            lines.append(f"> {genres_text}")
        return "\n".join(lines)

    score = score_or_missing(discography.get("artist_user_score"))

    ratings_count = (
        discography.get(
            "artist_ratings_count"
        )
        or "0"
    )

    followers = (
        discography.get(
            "artist_followers"
        )
        or "0"
    )

    lines = [
        (
            f"<:aoty:1539095897084924004> {score} • "
            f"**{ratings_count} ratings • {followers} followers**"
        )
    ]

    genres_text = ", ".join(display_genres(discography.get("genres") or []))
    genres_text = genres_text or discography.get("genres_text")

    if genres_text:
        # The line already follows the score summary; a ``Genre:`` label made
        # long cached genre lists wrap unnecessarily.
        lines.append(genres_text)

    musicbrainz_data = (discography.get("source_data") or {}).get("musicbrainz") or {}
    country_code = musicbrainz_data.get("country")
    country_name = (
        musicbrainz.display_origin_area(
            musicbrainz_data.get("origin_area"),
            country_code,
        )
        or country_code
    )
    flag = _country_flag(country_code)
    if country_name:
        lines.append(
            f"**Origin:** {flag + ' ' if flag else ''}{country_name}"
        )

    relation_label = discography.get(
        "relation_label"
    )

    relation_text = _artist_relation_text(
        discography.get(
            "relation"
        )
    )

    if relation_label and relation_text:
        lines.append(
            f"**{relation_label}:** {relation_text}"
        )

    akas_text = discography.get(
        "akas_text"
    )

    if akas_text:
        lines.append(
            f"**AKAs:** {akas_text}"
        )

    return "\n".join(
        lines
    )



class ArtistFormatSelect(discord.ui.Select):
    """Format filter.

    WAŻNE:
    Pokazuje WSZYSTKIE formaty obsługiwane przez bota, nawet jeśli wybrany
    artysta nie ma żadnej pozycji danego typu.
    """

    def __init__(self, artist_view):
        self.artist_view = artist_view

        super().__init__(
            placeholder="Format: Wszystkie formaty",
            min_values=1,
            max_values=1,
            options=[],
            row=2,
        )

        self.refresh_options()

    def refresh_options(self):
        view = self.artist_view

        options = [
            discord.SelectOption(
                label="Wszystkie formaty",
                value="all",
                default=view.selected_format == "all",
            )
        ]

        # Nie filtrujemy tej listy według dyskografii artysty.
        # Wszystkie formaty z settings.py są zawsze dostępne.
        for key, info in RATING_FORMATS.items():
            options.append(
                discord.SelectOption(
                    label=info["label"],
                    value=key,
                    default=view.selected_format == key,
                )
            )

        self.options = options[:25]
        self.placeholder = (
            f"Format: {view.selected_format_label}"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        view = self.artist_view
        view.selected_format = self.values[0]
        view.reset_page()

        if view.selected_year not in {None, *view.available_years()}:
            view.selected_year = None
        if view.selected_decade not in {None, *view.available_decades()}:
            view.selected_decade = None

        view.refresh_controls()

        embed = await view.build_embed()

        await interaction.edit_original_response(
            embed=embed,
            view=view,
        )


class ArtistPeriodSelect(discord.ui.Select):
    """Filtr okresu z konkretnymi latami oraz kompaktowymi dekadami."""

    def __init__(self, artist_view):
        self.artist_view = artist_view
        super().__init__(
            placeholder="Lata: Wszystkie lata",
            min_values=1,
            max_values=1,
            options=[],
            row=3,
        )
        self.refresh_options()

    def refresh_options(self):
        view = self.artist_view
        years = view.available_years()
        decades = view.available_decades()
        if view.selected_year not in {None, *years}:
            view.selected_year = None
        if view.selected_decade not in {None, *decades}:
            view.selected_decade = None

        # Discord pozwala na 25 opcji. Pokazujemy najnowsze konkretne lata
        # (najczęstszy wybór) i równolegle dekady dla starszej dyskografii.
        # Aktywny filtr zawsze pozostaje widoczny, nawet gdy jest bardzo stary.
        visible_years = list(years)
        if view.selected_year in visible_years:
            visible_years.remove(view.selected_year)
            visible_years.insert(0, view.selected_year)
        visible_decades = list(decades)
        if view.selected_decade in visible_decades:
            visible_decades.remove(view.selected_decade)
            visible_decades.insert(0, view.selected_decade)

        self.options = [
            discord.SelectOption(
                label="Wszystkie lata",
                value="all",
                default=view.selected_year is None and view.selected_decade is None,
            )
        ] + [
            discord.SelectOption(
                label=str(year),
                value=f"year:{year}",
                default=view.selected_year == year,
            )
            for year in visible_years[:12]
        ] + [
            discord.SelectOption(
                label=f"{start}-{start + 9}",
                value=f"decade:{start}",
                default=view.selected_decade == start,
            )
            for start in visible_decades[:12]
        ]
        self.placeholder = f"Lata: {view.selected_period_label}"
        self.disabled = not years

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value = self.values[0]
        view = self.artist_view
        view.selected_year = None
        view.selected_decade = None
        if value.startswith("year:"):
            view.selected_year = value.split(":", 1)[1]
        elif value.startswith("decade:"):
            view.selected_decade = int(value.split(":", 1)[1])
        view.reset_page()
        view.refresh_controls()
        await interaction.edit_original_response(
            embed=await view.build_embed(),
            view=view,
        )


class ArtistGenreSelect(discord.ui.Select):
    """Primary-genre filter sourced entirely from cached release metadata."""

    def __init__(self, artist_view):
        self.artist_view = artist_view
        super().__init__(
            placeholder="Gatunek: Wszystkie gatunki",
            min_values=1,
            max_values=1,
            options=[],
            row=4,
        )
        self.refresh_options()

    def refresh_options(self):
        view = self.artist_view
        genres = view.available_genres()
        if view.selected_genre not in {"all", *genres}:
            view.selected_genre = "all"
        self.options = [
            discord.SelectOption(
                label="Wszystkie gatunki",
                value="all",
                default=view.selected_genre == "all",
            )
        ] + [
            discord.SelectOption(
                label=genre[:100],
                value=genre[:100],
                default=view.selected_genre == genre,
            )
            for genre in genres[:24]
        ]
        self.placeholder = (
            "Gatunek: Wszystkie gatunki"
            if view.selected_genre == "all"
            else f"Gatunek: {view.selected_genre}"
        )
        self.disabled = not genres

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = self.artist_view
        view.selected_genre = self.values[0]
        view.reset_page()
        if view.selected_year not in {None, *view.available_years()}:
            view.selected_year = None
        if view.selected_decade not in {None, *view.available_decades()}:
            view.selected_decade = None
        view.refresh_controls()
        await interaction.edit_original_response(
            embed=await view.build_embed(),
            view=view,
        )


class ArtistSortView(TimedDisableView):
    """Interactive /artist sorter + format/year filters."""

    def __init__(
        self,
        *,
        discography,
        releases,
        selected_format="all",
        selected_genre="all",
        selected_year=None,
        decade=None,
        aoty_min=None,
        aoty_max=None,
        timeout=ARTIST_VIEW_TIMEOUT_SECONDS,
        owner_id: int | None = None,
    ):
        super().__init__(
            timeout=timeout,
            owner_id=owner_id,
        )

        self.discography = discography
        self.releases = _prepare_release_order(
            releases
        )

        self.sort_key = "newest"
        self.selected_format = selected_format
        self.selected_genre = selected_genre or "all"
        self.selected_year = str(selected_year) if selected_year is not None else None
        self.selected_decade = int(decade) if decade is not None else None
        self.aoty_min = aoty_min
        self.aoty_max = aoty_max
        # Pages are calculated from the locally cached discography; switching
        # them never asks AOTY for more data.
        self.page_index = 0

        # Cache public release details inside this message.
        # Switching filters/sort does not repeatedly fetch the same release.
        self._variables_cache = {}

        self.format_select = ArtistFormatSelect(
            self
        )

        self.period_select = ArtistPeriodSelect(
            self
        )

        self.genre_select = ArtistGenreSelect(self)

        self.add_item(
            self.format_select
        )

        self.add_item(
            self.period_select
        )
        self.add_item(self.genre_select)

        self.refresh_controls()

    @property
    def selected_format_label(self):
        if self.selected_format == "all":
            return "Wszystkie formaty"

        info = RATING_FORMATS.get(
            self.selected_format
        )

        if not info:
            return self.selected_format

        return info["label"]

    @property
    def selected_period_label(self):
        if self.selected_year is not None:
            return self.selected_year
        if self.selected_decade is None:
            return "Wszystkie lata"
        return f"{self.selected_decade}-{self.selected_decade + 9}"

    @property
    def selected_genre_label(self):
        return (
            "Wszystkie gatunki"
            if self.selected_genre == "all"
            else self.selected_genre
        )

    def _format_filtered_releases(self, *, include_base: bool = True):
        releases = self._base_filtered_releases() if include_base else list(self.releases)
        if self.selected_format == "all":
            return releases

        return [
            release
            for release in releases
            if _format_key_for_release(
                release
            ) == self.selected_format
        ]

    def _base_filtered_releases(self):
        releases = list(self.releases)
        if self.selected_year is not None:
            releases = [
                release
                for release in releases
                if _release_year(release) == self.selected_year
            ]
        elif self.selected_decade is not None:
            start = int(self.selected_decade)
            releases = [
                release
                for release in releases
                if (
                    _release_year(release) is not None
                    and start <= int(_release_year(release)) <= start + 9
                )
            ]
        if self.aoty_min is not None:
            releases = [
                release for release in releases
                if (
                    _score_number(release.get("user_score")) is not None
                    and _score_number(release.get("user_score")) >= int(self.aoty_min)
                )
            ]
        if self.aoty_max is not None:
            releases = [
                release for release in releases
                if (
                    _score_number(release.get("user_score")) is not None
                    and _score_number(release.get("user_score")) <= int(self.aoty_max)
                )
            ]
        return releases

    def _format_and_genre_filtered_releases(self, *, include_base: bool = True):
        releases = self._format_filtered_releases(include_base=include_base)
        if self.selected_genre == "all":
            return releases
        selected = self.selected_genre.casefold()
        return [
            release
            for release in releases
            if any(
                genre.casefold() == selected
                for genre in display_genres(release.get("genres") or [])
            )
        ]

    def available_genres(self):
        return sorted(
            {
                genre
                for release in self._format_filtered_releases()
                for genre in display_genres(release.get("genres") or [])
            },
            key=str.casefold,
        )

    def available_decades(self):
        decades = {
            (int(year) // 10) * 10
            for year in (
                _release_year(release)
                for release in self._format_and_genre_filtered_releases(
                    include_base=False
                )
            )
            if year is not None
        }
        return sorted(decades, reverse=True)

    def available_years(self):
        """Konkretne lata po filtrach formatu i gatunku, bez filtra okresu."""

        years = {
            year
            for year in (
                _release_year(release)
                for release in self._format_and_genre_filtered_releases(
                    include_base=False
                )
            )
            if year is not None
        }
        return sorted(years, reverse=True)

    def filtered_releases(self):
        releases = self._format_and_genre_filtered_releases()

        return releases

    def _sorted_filtered_releases(self):
        """Return the selected local catalogue in the active sort order."""

        return _sort_releases(self.filtered_releases(), self.sort_key)

    def _page_count(self) -> int:
        total = len(self._sorted_filtered_releases())
        return max(1, (total + MAX_ARTIST_RELEASES - 1) // MAX_ARTIST_RELEASES)

    def reset_page(self) -> None:
        """Return to the beginning after any sort or filter adjustment."""

        self.page_index = 0

    def _refresh_pagination_controls(self) -> None:
        """Show arrows only when the current filter needs more than one page."""

        page_count = self._page_count()
        self.page_index = min(max(self.page_index, 0), page_count - 1)
        controls = (self.previous_page_button, self.next_page_button)

        if page_count == 1:
            for control in controls:
                if control in self.children:
                    self.remove_item(control)
            return

        for control in controls:
            if control not in self.children:
                self.add_item(control)
        self.previous_page_button.disabled = self.page_index == 0
        self.next_page_button.disabled = self.page_index >= page_count - 1

    def refresh_controls(self):
        self._refresh_button_styles()
        self._refresh_pagination_controls()
        self.format_select.refresh_options()
        self.period_select.refresh_options()
        self.genre_select.refresh_options()

    def _refresh_button_styles(self):
        # One button represents both sort directions.  Its text reflects the
        # active direction and the blue state marks the active sort family.
        pairs = (
            (self.score_desc_button, "score_desc", "score_asc", "Ocena ↓", "Ocena ↑"),
            (self.title_asc_button, "title_asc", "title_desc", "A–Z", "Z–A"),
            (self.newest_button, "newest", "oldest", "Najnowsze", "Najstarsze"),
        )
        for button, primary, alternate, primary_label, alternate_label in pairs:
            is_active = self.sort_key in {primary, alternate}
            button.style = (
                discord.ButtonStyle.primary
                if is_active
                else discord.ButtonStyle.secondary
            )
            button.label = alternate_label if self.sort_key == alternate else primary_label

        for button in (self.previous_page_button, self.next_page_button):
            button.style = discord.ButtonStyle.secondary

    async def _variables_for_release(self, release):
        release_id = str(
            release.get("album_id")
            or release.get("url")
            or release.get("title")
            or id(release)
        )

        cached = self._variables_cache.get(
            release_id
        )

        if cached is not None:
            return cached

        # The artist page already contains the compact release information
        # needed by this list. Do NOT fetch 18 individual album pages here.
        variables = build_release_variables(
            release,
            release,
        )

        self._variables_cache[
            release_id
        ] = variables

        return variables

    async def build_embed(self):
        filtered = self.filtered_releases()
        sorted_releases = self._sorted_filtered_releases()
        page_count = self._page_count()
        self.page_index = min(self.page_index, page_count - 1)
        page_start = self.page_index * MAX_ARTIST_RELEASES
        shown = sorted_releases[page_start : page_start + MAX_ARTIST_RELEASES]

        lines = []

        for release in shown:
            variables = await self._variables_for_release(
                release
            )

            lines.append(
                f"{aoty_score_or_missing(variables.aoty_user_score, variables.ratings_count)} • "
                f"**{must_hear_title_marker(variables)} [{variables.display_album}]({release['url']})** • "
                f"{variables.album_format} • {variables.release_date}"
            )


        if lines:
            releases_text = "\n".join(
                lines
            )
        else:
            releases_text = (
                "Brak wydań dla wybranych filtrów."
            )

        header_text = _artist_header_text(
            self.discography
        )

        description = (
            f"{header_text}\n\n{releases_text}"
            if header_text
            else releases_text
        )

        embed = discord.Embed(
            title=display_romanized_name(
                self.discography["artist"]
            ),
            url=self.discography["url"],
            description=description,
        )

        if self.discography.get("image"):
            embed.set_thumbnail(
                url=self.discography["image"]
            )

        sort_label = SORT_LABELS.get(
            self.sort_key,
            self.sort_key,
        )

        filter_text = (
            f"{self.selected_format_label}  •  "
            f"{self.selected_genre_label}  •  "
            f"{self.selected_period_label}"
        )
        extra_filters = []
        if self.aoty_min is not None or self.aoty_max is not None:
            extra_filters.append(
                f"AOTY {self.aoty_min if self.aoty_min is not None else '—'}"
                f"–{self.aoty_max if self.aoty_max is not None else '—'}"
            )
        if extra_filters:
            filter_text += "  •  " + "  •  ".join(extra_filters)

        if len(filtered) > len(shown):
            footer = (
                f"{filter_text}  •  "
                f"pokazano {page_start + 1}–{page_start + len(shown)} "
                f"z {len(filtered)} wydań • strona "
                f"{self.page_index + 1}/{page_count}."
            )
        else:
            footer = (
                f"{filter_text}  •  "
                f"{len(shown)} wydań."
            )

        set_aoty_footer(embed, footer)

        return embed

    async def _apply_sort(
        self,
        interaction,
        sort_key,
    ):
        await interaction.response.defer()

        # Each of the three controls is a direction toggle.  It replaces the
        # former doubled set of six sorting buttons without losing a mode.
        pairs = {
            "score_desc": ("score_desc", "score_asc"),
            "title_asc": ("title_asc", "title_desc"),
            "newest": ("newest", "oldest"),
        }
        primary, alternate = pairs[sort_key]
        self.sort_key = alternate if self.sort_key == primary else primary
        self.reset_page()
        self.refresh_controls()

        embed = await self.build_embed()

        await interaction.edit_original_response(
            embed=embed,
            view=self,
        )

    @discord.ui.button(
        label="←",
        style=discord.ButtonStyle.secondary,
        custom_id="artist_previous_page",
        row=1,
    )
    async def previous_page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer()
        self.page_index = max(0, self.page_index - 1)
        self.refresh_controls()
        await interaction.edit_original_response(
            embed=await self.build_embed(),
            view=self,
        )

    @discord.ui.button(
        label="→",
        style=discord.ButtonStyle.secondary,
        custom_id="artist_next_page",
        row=1,
    )
    async def next_page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer()
        self.page_index = min(self._page_count() - 1, self.page_index + 1)
        self.refresh_controls()
        await interaction.edit_original_response(
            embed=await self.build_embed(),
            view=self,
        )

    @discord.ui.button(
        label="Ocena ↓",
        style=discord.ButtonStyle.secondary,
        custom_id="score_desc",
        row=0,
    )
    async def score_desc_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_sort(
            interaction,
            "score_desc",
        )

    @discord.ui.button(
        label="A–Z",
        style=discord.ButtonStyle.secondary,
        custom_id="title_asc",
        row=0,
    )
    async def title_asc_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_sort(
            interaction,
            "title_asc",
        )

    @discord.ui.button(
        label="Najnowsze",
        style=discord.ButtonStyle.primary,
        custom_id="newest",
        row=0,
    )
    async def newest_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_sort(
            interaction,
            "newest",
        )


async def build_artist_response(
    artist: str,
    *,
    format_key: str = "all",
    genre: str | None = None,
    year: int | None = None,
    decade: int | None = None,
    aoty_min: int | None = None,
    aoty_max: int | None = None,
    owner_id: int | None = None,
) -> tuple[discord.Embed, ArtistSortView] | None:
    """Build exactly the same interactive artist result used by /artist."""

    artist_info, discography = await DATA.get_artist_discography(artist)
    if not artist_info or not discography:
        return None

    releases = list(discography.get("releases", []))
    if not releases:
        return None

    view = ArtistSortView(
        discography=discography,
        releases=releases,
        selected_format=format_key,
        selected_genre=genre or "all",
        selected_year=year,
        decade=decade,
        aoty_min=aoty_min,
        aoty_max=aoty_max,
        owner_id=owner_id,
    )
    embed = await view.build_embed()
    return embed, view


def setup_artist_command(
    tree: discord.app_commands.CommandTree
):
    async def artist_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ):
        if not current or len(current.strip()) < 2:
            return []

        results = await DATA.search_artists(current, limit=10)

        choices = []

        for item in results[:10]:
            display_name = display_romanized_name(
                item["name"]
            )

            matched_aka = item.get(
                "matched_aka"
            )

            if matched_aka:
                aka_display = display_romanized_name(
                    matched_aka
                )

                label = (
                    f"{display_name} — AKA: "
                    f"{aka_display}"
                )
            else:
                label = display_name

            choices.append(
                discord.app_commands.Choice(
                    name=label[:100],
                    value=item["value"][:100],
                )
            )

        return choices

    @tree.command(
        name="artist",
        description="Pokazuje dyskografię artysty z datami i ocenami AOTY",
    )
    @discord.app_commands.describe(
        artist="Nazwa artysty na AOTY (opcjonalnie; bez pola używa Rich Presence)",
        aoty_min="Minimalny AOTY User Score (0–100)",
        aoty_max="Maksymalny AOTY User Score (0–100)",
    )
    @discord.app_commands.autocomplete(artist=artist_autocomplete)
    async def artist_command(
        interaction: discord.Interaction,
        artist: str | None = None,
        aoty_min: int | None = None,
        aoty_max: int | None = None,
    ):
        if any(
            value is not None and not 0 <= value <= 100
            for value in (aoty_min, aoty_max)
        ):
            await interaction.response.send_message(
                "AOTY User Score musi mieścić się w zakresie 0–100.",
                ephemeral=True,
            )
            return
        if aoty_min is not None and aoty_max is not None and aoty_min > aoty_max:
            await interaction.response.send_message(
                "`aoty_min` nie może być większe niż `aoty_max`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()

        artist = str(artist or "").strip()
        if not artist:
            member = interaction.user
            if interaction.guild is not None:
                member = interaction.guild.get_member(interaction.user.id) or member
            presence = _music_from_presence(
                member,
                cached_activities=PRESENCE_CACHE.activities_for(interaction.user.id),
            )
            if presence is None:
                profile = KOTONE_USERS_BY_DISCORD_ID.get(interaction.user.id)
                scrobble = LASTFM_DB.latest_scrobble(
                    (profile or {}).get("name")
                )
                if not scrobble or not scrobble.get("artist"):
                    await interaction.followup.send(
                        "❌ Nie widzę aktywnego artysty w Rich Presence ani "
                        "zapisanego ostatniego scrobbla Last.fm."
                    )
                    return
                artist = str(scrobble["artist"])
                source = "Last.fm (ostatni scrobble)"
            else:
                artist, _album, source = presence
            print(f"[ARTIST] Rich Presence ({source}): {artist}")

        try:
            result = await build_artist_response(
                artist,
                aoty_min=aoty_min,
                aoty_max=aoty_max,
                owner_id=interaction.user.id,
            )
            if result is None:
                await interaction.followup.send(
                    f"❌ Nie znaleziono artysty **{artist}** na AOTY ani w kotone."
                )
                return

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

        embed, view = result

        message = await interaction.followup.send(
            embed=embed,
            view=view,
            wait=True,
        )

        view.bind_message(
            message
        )
