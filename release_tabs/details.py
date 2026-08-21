"""Wspólna zakładka informacji o wydaniu."""

from __future__ import annotations

import re
import unicodedata
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
_RYM_BASE = "https://rateyourmusic.com/release"
_RYM_FORMATS = {
    "lp": "album",
    "album": "album",
    "ep": "ep",
    "single": "single",
    "music_video": "music-video",
    "music video": "music-video",
    "video": "music-video",
    "mixtape": "mixtape",
    "dj_mix": "dj-mix",
    "dj mix": "dj-mix",
    "compilation": "compilation",
    "comp": "compilation",
}


def _markdown_link(text: object, url: object) -> str:
    rendered = str(text or "").strip()
    target = str(url or "").strip()
    if not rendered or not target.startswith(("http://", "https://")):
        return rendered
    return f"[{rendered}]({target})"


def _rym_slug(value: object) -> str:
    """Produce the stable, human-readable path segment used by RYM URLs."""

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _rym_fallback_url(variables: ReleaseVariables) -> str | None:
    """Build a RYM release URL only when all three route parts are known."""

    release_type = _RYM_FORMATS.get(
        str(variables.album_format or "").strip().casefold()
    )
    artist = _rym_slug(variables.display_artist or variables.artist)
    album = _rym_slug(variables.display_album or variables.album)
    if not release_type or not artist or not album:
        return None
    return f"{_RYM_BASE}/{release_type}/{artist}/{album}/"


def _ratings_url(album_url: str) -> str | None:
    target = str(album_url or "").strip().rstrip("/")
    if not target or "albumoftheyear.org/album/" not in target:
        return None
    if target.endswith(".php"):
        target = target[:-4]
    return f"{target}/user-reviews/?type=ratings"


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


def _label_slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _label_url_slug(url: object) -> str:
    """Wyciągnij slug labelu z URL-a AOTY, np. ``74-4ad``."""

    match = re.search(r"/label/(?:\d+-)?([^/?#]+)/?", str(url or "").casefold())
    return _label_slug(match.group(1)) if match else ""


def _linked_label(labels: list[str], labels_text: object, url: object) -> str:
    """Linkuj label tylko wtedy, gdy URL wskazuje właśnie na ten label."""

    rendered = str(labels_text or "").strip()
    target = str(url or "").strip()
    if not rendered or not target.startswith(("http://", "https://")):
        return rendered
    label_names = labels or [part.strip() for part in rendered.split(",") if part.strip()]
    target_slug = _label_url_slug(target)
    if target_slug and any(_label_slug(name) == target_slug for name in label_names):
        return _markdown_link(rendered, target)
    return rendered


def _details_title(variables: ReleaseVariables) -> str:
    """Zbuduj tytuł embeda z limitem Discorda 256 znaków.

    Tytuł ma jeden link ustawiony przez ``embed.url``. Discord nie renderuje
    Markdown w tytułach, więc nie tworzymy linków dla pojedynczych fragmentów.
    """

    tab = status_emoji("details") or DETAILS_BUTTON
    artist_name = str(variables.display_artist or MISSING_VALUE).strip()
    album_name = str(variables.display_album or MISSING_VALUE).strip()
    marker = must_hear_title_marker(variables)
    title = f"{tab} {artist_name} — {marker + ' ' if marker else ''}{album_name}".strip()
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
            value=", ".join(variables.genres) if variables.genres else MISSING_VALUE,
        ),
        _detail_line(
            variables,
            section="genres",
            label="Secondary genres",
            value=(", ".join(variables.secondary_genres)
                   if variables.secondary_genres else MISSING_VALUE),
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
        # historii. Starsze importy mogły być zapisane pod nazwą Kotone,
        # dlatego najpierw znajdujemy faktycznie użyty klucz archiwum.
        archive_key = lastfm_username
        for candidate in dict.fromkeys((lastfm_username, str(name))):
            profile_data = LASTFM_DB.get_profile(candidate)
            progress = LASTFM_DB.archive_progress(candidate)
            if profile_data is not None or int(progress.get("scrobbles") or 0):
                archive_key = candidate
                break
        else:
            profile_data = LASTFM_DB.get_profile(archive_key)
            progress = LASTFM_DB.archive_progress(archive_key)
        if profile_data is None and not int(progress.get("scrobbles") or 0):
            continue
        count = LASTFM_DB.album_scrobble_count(
            archive_key,
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
    rym_url = str(
        musicbrainz_data.get("rym_url")
        or musicbrainz_data.get("rateyourmusic_url")
        or discogs_data.get("rym_url")
        or ""
    ).strip()
    rym_url = rym_url or _rym_fallback_url(variables) or ""
    album_id = display_value(variables.album_id)
    discogs_id = display_value(discogs_data.get("discogs_release_id"))
    musicbrainz_release_id = display_value(musicbrainz_data.get("musicbrainz_release_id"))
    musicbrainz_group_id = display_value(
        musicbrainz_data.get("musicbrainz_release_group_id")
    )
    discogs_url = str(
        discogs_data.get("discogs_master_url")
        or discogs_data.get("discogs_url")
        or musicbrainz_data.get("discogs_master_url")
        or musicbrainz_data.get("discogs_url")
        or ""
    ).strip()
    if discogs_id == MISSING_VALUE and discogs_url:
        match = re.search(r"/(?:release|master)/(\d+)", discogs_url)
        if match:
            discogs_id = match.group(1)
    return [
        f"{source_emoji('rym')} "
        f"{_markdown_link('RateYourMusic', rym_url) if rym_url else 'RateYourMusic: —'}",
        f"{source_emoji('aoty')} **Album ID:** "
        f"{_markdown_link(album_id, variables.album_url or variables.url)}",
        f"{source_emoji('discogs')} **Release ID:** "
        f"{_markdown_link(discogs_id, discogs_url or (f'https://www.discogs.com/release/{quote(discogs_id)}' if discogs_id != MISSING_VALUE else None))}",
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

    embed = discord.Embed(
        title=_details_title(variables),
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
    # Ocena jest już widoczna w treści zakładki; nie powtarzaj jej jako
    # custom emoji w stopce.
    footer = "AOTY"
    if page_count and page_count > 1 and page_number:
        footer = f"{footer} • [{page_number}/{page_count}]"
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
