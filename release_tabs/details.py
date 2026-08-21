"""Wspólna zakładka informacji o wydaniu."""

from __future__ import annotations

import re
from urllib.parse import quote

import discord

from lastfm_globals import LASTFM_DB
from release_tabs.common import (
    MISSING_VALUE,
    apply_release_identity,
    display_value,
    paginate_description_lines,
)
from shared import (
    ReleaseVariables,
    aoty_score_value,
    aoty_score_or_missing,
    country_flag_emoji,
    load_release_variables,
    score_color,
    score_or_nr,
    score_or_missing,
    set_aoty_footer,
    source_emoji,
    must_hear_title_marker,
    user_avatar_emoji,
)
from settings import KOTONE_USERS
from ui_constants import DETAILS_BUTTON
from ui_constants import MUST_HEAR_EMOJIS
from status_emoji_registry import status_emoji


# ---------------------------------------------------------------------------
# Formatowanie pojedynczych wierszy
# ---------------------------------------------------------------------------

_AOTY_BASE = "https://www.albumoftheyear.org"


def _markdown_link(text: object, url: object) -> str:
    rendered = str(text or "").strip()
    target = str(url or "").strip()
    if not rendered or not target.startswith(("http://", "https://")):
        return rendered
    return f"[{rendered}]({target})"


def _ratings_url(album_url: str) -> str | None:
    target = str(album_url or "").strip().rstrip("/")
    if not target or "albumoftheyear.org/album/" not in target:
        return None
    if target.endswith(".php"):
        target = target[:-4]
    return f"{target}/user-reviews/?type=ratings"


def _must_hear_url(kind: str | None) -> str | None:
    if kind not in {"users", "critics", "both"}:
        return None
    return f"{_AOTY_BASE}/must-hear/{kind}/"


def _ranking_number(value: object) -> int | None:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def _ranking_url(value: object, *, year: object = None, all_time: bool = False) -> str | None:
    rank = _ranking_number(value)
    if rank is None:
        return None
    if all_time:
        page = ((rank - 1) // 25) + 1
        return f"{_AOTY_BASE}/ratings/user-highest-rated/all/{page}/#rank-{rank}"
    year_text = str(year or "").strip()
    if not re.fullmatch(r"\d{4}", year_text):
        return None
    return f"{_AOTY_BASE}/ratings/user-highest-rated/{year_text}/#rank-{rank}"


def _release_month_url(value: object) -> str | None:
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", str(value or "").strip())
    if not match:
        return None
    _, month, year = match.groups()
    month_names = (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    )
    month_index = int(month) - 1
    if not 0 <= month_index < len(month_names):
        return None
    month_name = month_names[month_index]
    return f"{_AOTY_BASE}/{year}/releases/{month_name}-{month}/"


def _vibe_url(value: object) -> str | None:
    text = str(value or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return f"{_AOTY_BASE}/all/releases/vibe/{quote(slug)}/" if slug else None


def _genre_slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _genre_url_slug(url: object) -> str:
    """Wyciągnij slug gatunku z URL-a AOTY, np. ``39-dream-pop``."""

    match = re.search(r"/genre/(?:\d+-)?([^/?#]+)/?", str(url or "").casefold())
    return _genre_slug(match.group(1)) if match else ""


def _label_url_slug(url: object) -> str:
    """Wyciągnij slug labelu z URL-a AOTY, np. ``74-4ad``."""

    match = re.search(r"/label/(?:\d+-)?([^/?#]+)/?", str(url or "").casefold())
    return _genre_slug(match.group(1)) if match else ""


def _linked_label(labels: list[str], labels_text: object, url: object) -> str:
    """Linkuj label tylko wtedy, gdy URL wskazuje właśnie na ten label."""

    rendered = str(labels_text or "").strip()
    target = str(url or "").strip()
    if not rendered or not target.startswith(("http://", "https://")):
        return rendered
    label_names = labels or [part.strip() for part in rendered.split(",") if part.strip()]
    target_slug = _label_url_slug(target)
    if target_slug and any(_genre_slug(name) == target_slug for name in label_names):
        return _markdown_link(rendered, target)
    return rendered


def _linked_genres(values: list[str], urls: list[str], *, bold_first: bool = False) -> str:
    """Linkuj gatunki po nazwie, nigdy po samej pozycji na liście.

    Kolejność danych z AOTY, MusicBrainz i ręcznego wpisu może być różna.
    Jeśli URL nie pasuje do konkretnego gatunku, pokazujemy nazwę bez linku
    zamiast kierować użytkownika do niewłaściwej strony AOTY.
    """

    parts: list[str] = []
    available = [str(url or "").strip() for url in urls if str(url or "").strip()]
    for index, value in enumerate(values):
        value_slug = _genre_slug(value)
        matching_url = next(
            (url for url in available if _genre_url_slug(url) == value_slug),
            None,
        )
        linked = _markdown_link(value, matching_url)
        parts.append(f"**{linked}**" if bold_first and index == 0 else linked)
    return ", ".join(parts) if parts else MISSING_VALUE


def _details_title(variables: ReleaseVariables) -> str:
    """Zbuduj tytuł embeda z limitem Discorda 256 znaków.

    Link Markdown zawiera długi URL, dlatego przy bardzo długich nazwach
    najpierw przechodzimy na krótszą wersję bez linków, a dopiero na końcu
    bezpiecznie skracamy tekst. Dzięki temu pojedynczy rekord nie może
    przerwać wysyłania całej zakładki `details`.
    """

    tab = status_emoji("details") or DETAILS_BUTTON
    artist_name = str(variables.display_artist or MISSING_VALUE).strip()
    album_name = str(variables.display_album or MISSING_VALUE).strip()
    artist = _markdown_link(artist_name, variables.artist_url)
    album = _markdown_link(album_name, variables.album_url or variables.url)
    kind = str(variables.must_hear_kind or "").casefold()
    marker = must_hear_title_marker(variables)
    if marker and (target := _must_hear_url(kind)):
        marker = _markdown_link(marker, target)
    title = f"{tab} {artist} — {marker + ' ' if marker else ''}{album}".strip()
    if len(title) <= 256:
        return title

    # URL-e w Markdown są tylko dodatkiem; zachowaj czytelne nazwy, gdy
    # pełna wersja nie mieści się w limicie.
    short_marker = must_hear_title_marker(variables)
    title = f"{tab} {artist_name} — {short_marker + ' ' if short_marker else ''}{album_name}".strip()
    if len(title) <= 256:
        return title
    return title[:253].rstrip() + "..."

def _source_prefix(
    variables: ReleaseVariables,
    section: str,
    value: object,
) -> str:
    """Zwróć emoji źródła tylko przy istniejącej wartości."""

    if display_value(value) == MISSING_VALUE:
        return ""
    source = str(variables.metadata_sources.get(section) or "aoty").casefold()
    emoji = source_emoji(source)
    return f"{emoji} " if emoji else ""


def _detail_line(
    variables: ReleaseVariables,
    *,
    section: str,
    label: str,
    value: object,
) -> str:
    return (
        f"{_source_prefix(variables, section, value)}"
        f"**{label}:** {display_value(value)}"
    )


def _provider_line(source: str, label: str, value: object) -> str | None:
    rendered = display_value(value)
    if rendered == MISSING_VALUE:
        return None
    prefix = source_emoji(source)
    return f"{prefix} **{label}:** {rendered}".lstrip()


# ---------------------------------------------------------------------------
# Sekcje opisu
# ---------------------------------------------------------------------------

def _score_section(variables: ReleaseVariables) -> list[str]:
    aoty_score = aoty_score_or_missing(
        variables.aoty_user_score,
        variables.ratings_count,
    )
    critic_score = score_or_missing(variables.critic_score)
    must_hear_kind = str(variables.must_hear_kind or "").casefold()
    must_hear_value = (
        f"{MUST_HEAR_EMOJIS[must_hear_kind]} {must_hear_kind}"
        if variables.must_hear and must_hear_kind in MUST_HEAR_EMOJIS
        else MISSING_VALUE
    )
    return [
        _detail_line(
            variables,
            section="score",
            label="User score",
            value=aoty_score,
        ),
        _detail_line(
            variables,
            section="score",
            label="Ratings",
            value=_markdown_link(
                variables.ratings_count,
                _ratings_url(variables.album_url or variables.url),
            ),
        ),

        _detail_line(
            variables,
            section="score",
            label="Critic score",
            value=critic_score,
        ),
        _detail_line(
            variables,
            section="score",
            label="Critic reviews",
            value=variables.critic_reviews_count,
        ),
        *_ranking_section(variables),
        _detail_line(
            variables,
            section="score",
            label="Must hear",
            value=must_hear_value,
        ),
    ]

def _ranking_section(variables: ReleaseVariables) -> list[str]:
    """Rankingi należą do bloku ocen, bez osobnego nagłówka."""

    ranking_year = display_value(variables.ranking_year)
    if ranking_year == MISSING_VALUE:
        ranking_year = display_value(variables.year)
    label = f"{ranking_year} ratings" if ranking_year != MISSING_VALUE else "Year ratings"
    return [
        _detail_line(
            variables,
            section="ranking",
            label=label,
            value=_markdown_link(
                variables.year_ranking_text,
                _ranking_url(
                    variables.year_ranking_text,
                    year=variables.ranking_year or variables.year,
                ),
            ),
        ),
        _detail_line(
            variables,
            section="ranking",
            label="All-time ratings",
            value=_markdown_link(
                variables.all_time_ranking,
                _ranking_url(variables.all_time_ranking, all_time=True),
            ),
        ),
    ]


def _genres_section(variables: ReleaseVariables) -> list[str]:
    return [
        _detail_line(
            variables,
            section="genres",
            label="Genres",
            value=_linked_genres(
                variables.genres,
                variables.genre_urls,
                bold_first=True,
            ),
        ),
        _detail_line(
            variables,
            section="genres",
            label="Secondary genres",
            value=_linked_genres(
                variables.secondary_genres,
                variables.secondary_genre_urls,
            ),
        ),
        _detail_line(
            variables,
            section="vibes",
            label="Vibes",
            value=(
                ", ".join(
                    _markdown_link(value, _vibe_url(value))
                    for value in variables.vibes
                )
                if variables.vibes
                else MISSING_VALUE
            ),
        ),
    ]


def _release_section(variables: ReleaseVariables) -> list[str]:
    musicbrainz_data = variables.source_data.get("musicbrainz") or {}
    country_code = musicbrainz_data.get("release_country")
    country = country_flag_emoji(country_code) or display_value(country_code)
    country_line = (
        f"{source_emoji('musicbrainz')} **Country:** {country}"
        if country != MISSING_VALUE
        else "**Country:** —"
    )
    return [
        _detail_line(
            variables,
            section="release_date",
            label="Release date",
            value=_markdown_link(
                variables.release_date,
                _release_month_url(variables.release_date),
            ),
        ),
        _detail_line(
            variables,
            section="duration",
            label="Duration",
            value=f"`{variables.duration}`",
        ),
        _detail_line(
            variables,
            section="format",
            label="Format",
            value=variables.album_format,
        ),
        _detail_line(
            variables,
            section="labels",
            label="Label",
            value=_linked_label(
                variables.labels,
                variables.labels_text,
                variables.label_url,
            ),
        ),
        country_line,
    ]


def _lastfm_section(variables: ReleaseVariables) -> list[str]:
    """Pokaż tylko faktycznie posiadane dane Last.fm i liczniki Kotone."""

    source_data = variables.source_data.get("lastfm") or {}
    rows = (
        ("Listeners", source_data.get("listeners_count")),
        ("Scrobbles", source_data.get("playcount")),
    )
    lines = [
        line
        for label, value in rows
        if (line := _provider_line("lastfm", label, value))
    ]

    musicbrainz_data = variables.source_data.get("musicbrainz") or {}
    for name, profile in KOTONE_USERS.items():
        lastfm_username = str(profile.get("lastfm_username") or "").strip()
        if not lastfm_username:
            continue
        # Nie pokazujemy fikcyjnego zera przed pierwszym zapisem profilu lub
        # historii. Zero pozostaje prawidłową wartością, gdy archiwum istnieje.
        profile_data = LASTFM_DB.get_profile(lastfm_username)
        progress = LASTFM_DB.archive_progress(lastfm_username)
        if profile_data is None and not int(progress.get("scrobbles") or 0):
            continue
        count = LASTFM_DB.album_scrobble_count(
            lastfm_username,
            variables.album,
            artist=variables.artist,
            album_mbid=musicbrainz_data.get("musicbrainz_release_group_id"),
            aoty_album_id=variables.album_id,
        )
        avatar_key = str(profile.get("aoty_username") or name)
        avatar = user_avatar_emoji(avatar_key)
        prefix = " ".join(
            part for part in (source_emoji("lastfm"), avatar, str(name)) if part
        )
        lines.append(f"{prefix} **scrobbles:** {count}")
    return lines


def _identity_section(variables: ReleaseVariables) -> list[str]:
    """Zawsze ostatnia strona: trwałe identyfikatory providerów."""

    musicbrainz_data = variables.source_data.get("musicbrainz") or {}
    discogs_data = variables.source_data.get("discogs") or {}
    album_id = display_value(variables.album_id)
    discogs_id = display_value(discogs_data.get("discogs_release_id"))
    musicbrainz_release_id = display_value(musicbrainz_data.get("musicbrainz_release_id"))
    musicbrainz_group_id = display_value(
        musicbrainz_data.get("musicbrainz_release_group_id")
    )
    return [
        f"{source_emoji('aoty')} **Album ID:** "
        f"{_markdown_link(album_id, variables.album_url or variables.url)}",
        f"{source_emoji('discogs')} **Release ID:** "
        f"{_markdown_link(discogs_id, f'https://www.discogs.com/release/{quote(discogs_id)}' if discogs_id != MISSING_VALUE else None)}",
        f"{source_emoji('musicbrainz')} **Release ID:** "
        f"{_markdown_link(musicbrainz_release_id, f'https://musicbrainz.org/release/{quote(musicbrainz_release_id)}' if musicbrainz_release_id != MISSING_VALUE else None)}",
        f"{source_emoji('musicbrainz')} **Release Group ID:** "
        f"{_markdown_link(musicbrainz_group_id, f'https://musicbrainz.org/release-group/{quote(musicbrainz_group_id)}' if musicbrainz_group_id != MISSING_VALUE else None)}",
    ]


def _content_description_lines(variables: ReleaseVariables) -> list[str]:
    """Główne sekcje details, bez identyfikatorów z końcowej strony."""

    sections = [
        _score_section(variables),
        _genres_section(variables),
        _release_section(variables),
        _lastfm_section(variables),
    ]

    lines: list[str] = []
    for section in sections:
        if not section:
            continue
        if lines:
            lines.append("")
        lines.extend(section)
    return lines


# ---------------------------------------------------------------------------
# Publiczny renderer zakładki
# ---------------------------------------------------------------------------

def _build_release_details_embed(
    variables: ReleaseVariables,
    description: str,
    *,
    username: str | None,
    author_icon_url: str | None,
    page_number: int | None = None,
    page_count: int | None = None,
) -> discord.Embed:
    """Zbuduj jedną stronę wspólnej zakładki szczegółów."""

    title = _details_title(variables)
    if page_count and page_count > 1 and page_number:
        suffix = f"  •  [{page_number}/{page_count}]"
        title = title[: 256 - len(suffix)].rstrip() + suffix
    embed = discord.Embed(
        title=title,
        url=variables.url or None,
        description=description,
        color=score_color(variables.score or variables.aoty_user_score),
    )
    apply_release_identity(
        embed,
        variables,
        username=username,
        author_icon_url=author_icon_url,
    )
    footer = f"AOTY • {score_or_nr(variables.score)}" if username else "AOTY"
    set_aoty_footer(embed, footer)
    return embed


async def build_release_details_embeds(
    item: dict,
    *,
    username: str | None = None,
    author_icon_url: str | None = None,
) -> list[discord.Embed]:
    """Renderuj pełne informacje z SQLite, dzieląc je bez utraty danych."""

    variables = await load_release_variables(
        item,
        username=username,
        missing=MISSING_VALUE,
    )
    descriptions = paginate_description_lines(_content_description_lines(variables))
    # Identyfikatory są zawsze na osobnej, ostatniej stronie — nie mieszają
    # się z opisem wydania i nie znikają przez paginację wcześniejszych pól.
    descriptions.append("\n".join(_identity_section(variables)))
    return [
        _build_release_details_embed(
            variables,
            description,
            username=username,
            author_icon_url=author_icon_url,
            page_number=index,
            page_count=len(descriptions),
        )
        for index, description in enumerate(descriptions, start=1)
    ]


async def build_release_details_embed(
    item: dict,
    *,
    username: str | None = None,
    author_icon_url: str | None = None,
) -> discord.Embed:
    """Kompatybilny renderer pierwszej strony zakładki szczegółów."""

    return (await build_release_details_embeds(
        item,
        username=username,
        author_icon_url=author_icon_url,
    ))[0]
