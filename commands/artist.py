import discord
import requests

import aoty
from services import DATA
from display_utils import display_genres, display_romanized_name
from formats import RATING_FORMATS, format_key_from_label
from shared import build_release_variables, score_or_nr, score_or_missing, set_aoty_footer
from views import TimedDisableView


MAX_ARTIST_RELEASES = 18
ARTIST_VIEW_TIMEOUT_SECONDS = 30 * 60


SORT_LABELS = {
    "newest": "Najnowsze",
    "oldest": "Najstarsze",
    "score_desc": "Ocena ↓",
    "score_asc": "Ocena ↑",
    "title_asc": "A–Z",
    "title_desc": "Z–A",
}


def _score_number(value):
    """Convert AOTY User Score to int; NR/None always sorts last."""
    try:
        return int(value)
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


def _artist_header_text(discography):
    """Metadata block displayed above the releases in /artist."""
    if discography.get("source") == "kotone db":
        release_count = len(discography.get("releases") or [])
        lines = [
            f"💾 **Baza danych Kotone: {release_count} zapisanych wydań.**"
        ]
        genres_text = ", ".join(display_genres(discography.get("genres") or []))
        genres_text = genres_text or discography.get("genres_text")
        if genres_text:
            lines.append(f"**Genre:** {genres_text}")
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
            f"⭐ **User Score: {score}**"
            f"  •  **{ratings_count} ratings**"
            f"  •  **{followers} Followers**"
        )
    ]

    genres_text = ", ".join(display_genres(discography.get("genres") or []))
    genres_text = genres_text or discography.get("genres_text")

    if genres_text:
        lines.append(
            f"**Genre:** {genres_text}"
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

        if view.selected_decade not in {None, *view.available_decades()}:
            view.selected_decade = None

        view.refresh_controls()

        embed = await view.build_embed()

        await interaction.edit_original_response(
            embed=embed,
            view=view,
        )


class ArtistDecadeSelect(discord.ui.Select):
    """Release-period filter showing compact decade ranges."""

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
        decades = view.available_decades()
        if view.selected_decade not in {None, *decades}:
            view.selected_decade = None
        self.options = [
            discord.SelectOption(
                label="Wszystkie lata",
                value="all",
                default=view.selected_decade is None,
            )
        ] + [
            discord.SelectOption(
                label=f"{start}-{start + 9}",
                value=str(start),
                default=view.selected_decade == start,
            )
            for start in decades[:24]
        ]
        self.placeholder = (
            "Lata: Wszystkie lata"
            if view.selected_decade is None
            else f"Lata: {view.selected_decade}-{view.selected_decade + 9}"
        )
        self.disabled = not decades

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value = self.values[0]
        self.artist_view.selected_decade = (
            None if value == "all" else int(value)
        )
        self.artist_view.refresh_controls()
        await interaction.edit_original_response(
            embed=await self.artist_view.build_embed(),
            view=self.artist_view,
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
    ):
        super().__init__(
            timeout=timeout
        )

        self.discography = discography
        self.releases = _prepare_release_order(
            releases
        )

        self.sort_key = "newest"
        self.selected_format = selected_format
        self.selected_genre = selected_genre or "all"
        self.selected_decade = (
            int(decade)
            if decade is not None
            else (
                (int(selected_year) // 10) * 10
                if selected_year is not None
                else None
            )
        )
        self.aoty_min = aoty_min
        self.aoty_max = aoty_max

        # Cache public release details inside this message.
        # Switching filters/sort does not repeatedly fetch the same release.
        self._variables_cache = {}

        self.format_select = ArtistFormatSelect(
            self
        )

        self.decade_select = ArtistDecadeSelect(
            self
        )

        self.genre_select = ArtistGenreSelect(self)

        self.add_item(
            self.format_select
        )

        self.add_item(
            self.decade_select
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
        if self.selected_decade is not None:
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

    def filtered_releases(self):
        releases = self._format_and_genre_filtered_releases()

        return releases

    def refresh_controls(self):
        self._refresh_button_styles()
        self.format_select.refresh_options()
        self.decade_select.refresh_options()
        self.genre_select.refresh_options()

    def _refresh_button_styles(self):
        for child in self.children:
            if not isinstance(
                child,
                discord.ui.Button,
            ):
                continue

            if child.custom_id == self.sort_key:
                child.style = (
                    discord.ButtonStyle.primary
                )
            else:
                child.style = (
                    discord.ButtonStyle.secondary
                )

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

        sorted_releases = _sort_releases(
            filtered,
            self.sort_key,
        )

        shown = sorted_releases[
            :MAX_ARTIST_RELEASES
        ]

        lines = []

        for release in shown:
            variables = await self._variables_for_release(
                release
            )

            lines.append(
                f"**{score_or_missing(variables.aoty_user_score)}**  "
                f"**[{variables.display_album}]({release['url']})**"
                f"  —  {variables.release_date}  •  {variables.album_format}"
            )


        if lines:
            releases_text = "\n".join(
                lines
            )
        else:
            releases_text = (
                "Brak releases dla wybranego "
                "formatu i roku."
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
                f"pokazano {len(shown)} z {len(filtered)} releases."
            )
        else:
            footer = (
                f"{filter_text}  •  "
                f"{len(shown)} releases."
            )

        set_aoty_footer(embed, footer)

        return embed

    async def _apply_sort(
        self,
        interaction,
        sort_key,
    ):
        await interaction.response.defer()

        self.sort_key = sort_key
        self.refresh_controls()

        embed = await self.build_embed()

        await interaction.edit_original_response(
            embed=embed,
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
        label="Ocena ↑",
        style=discord.ButtonStyle.secondary,
        custom_id="score_asc",
        row=0,
    )
    async def score_asc_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_sort(
            interaction,
            "score_asc",
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
        label="Z–A",
        style=discord.ButtonStyle.secondary,
        custom_id="title_desc",
        row=0,
    )
    async def title_desc_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_sort(
            interaction,
            "title_desc",
        )

    @discord.ui.button(
        label="Najnowsze",
        style=discord.ButtonStyle.primary,
        custom_id="newest",
        row=1,
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

    @discord.ui.button(
        label="Najstarsze",
        style=discord.ButtonStyle.secondary,
        custom_id="oldest",
        row=1,
    )
    async def oldest_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_sort(
            interaction,
            "oldest",
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
        artist="Nazwa artysty na AOTY",
    )
    @discord.app_commands.autocomplete(artist=artist_autocomplete)
    async def artist_command(
        interaction: discord.Interaction,
        artist: str,
    ):
        await interaction.response.defer()

        try:
            result = await build_artist_response(
                artist,
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
