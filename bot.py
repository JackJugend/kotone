import asyncio
import json
import os
import re
import shutil


from datetime import datetime, timedelta
from urllib.parse import urljoin

import discord
import requests
from bs4 import BeautifulSoup

from commands.last import setup_last_command

# ============================================================
# KONFIGURACJA
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# Lokalnie bot dalej używa data.json obok bot.py.
# Na hostingu można ustawić DATA_DIR (np. /app/data na Railway),
# żeby pamięć bota była przechowywana na persistent volume.
DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data.json")
DATA_DIR = os.getenv("DATA_DIR")

if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
    DATA_FILE = os.path.join(DATA_DIR, "data.json")

    # Przy pierwszym starcie wolumenu zachowaj aktualny stan z paczki.
    if not os.path.exists(DATA_FILE) and os.path.exists(DEFAULT_DATA_FILE):
        shutil.copyfile(DEFAULT_DATA_FILE, DATA_FILE)
else:
    DATA_FILE = DEFAULT_DATA_FILE

BASE_URL = "https://www.albumoftheyear.org"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

TOKEN = os.getenv("DISCORD_TOKEN") or config.get("discord_token", "")

if not TOKEN:
    raise RuntimeError(
        "Brak tokenu Discord. Ustaw DISCORD_TOKEN albo discord_token w config.json."
    )
APPLICATION_ID = int(config["application_id"])
GUILD_ID = int(config["guild_id"])
CHANNEL_ID = int(config["channel_id"])
USERS = config["users"]

# Opcjonalna mapa: użytkownik AOTY -> kanał Discord.
# Jeśli użytkownika nie ma w mapie, bot użyje CHANNEL_ID jako fallbacku.
USER_CHANNELS = {
    username.lower(): int(channel_id)
    for username, channel_id in config.get("user_channels", {}).items()
}

CHECK_INTERVAL = int(
    config.get("check_interval", 300)
)

if CHECK_INTERVAL < 60:
    print(
        "[UWAGA] Interwał był mniejszy niż 60 sekund. "
        "Ustawiam 60 sekund."
    )
    CHECK_INTERVAL = 60

intents = discord.Intents.default()

activity = discord.Activity(
    type=discord.ActivityType.watching,
    name="AOTY.org"
)

client = discord.Client(
    intents=intents,
    application_id=APPLICATION_ID,
    activity=activity,
    status=discord.Status.idle
)

tree = discord.app_commands.CommandTree(client)

# ============================================================
# REQUESTS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# BŁĘDY
# ============================================================

class AOTYRateLimit(Exception):
    pass

class AOTYUserNotFound(Exception):
    pass


# ============================================================
# PAMIĘĆ
# ============================================================

STATE_VERSION = 4


def create_empty_state():
    return {
        "version": STATE_VERSION,
        "users": {}
    }


def load_state():

    if not os.path.exists(DATA_FILE):
        return create_empty_state()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)

    except Exception as e:
        print(
            f"[DATA] Błąd odczytu data.json: {e}"
        )
        return create_empty_state()

    if (
        not isinstance(loaded, dict)
        or loaded.get("version") != STATE_VERSION
        or not isinstance(loaded.get("users"), dict)
    ):

        try:
            shutil.copyfile(
                DATA_FILE,
                os.path.join(BASE_DIR, "data_old.json")
            )

            print(
                "[DATA] Stary data.json zapisano "
                "jako data_old.json."
            )

        except Exception:
            pass

        print(
            "[DATA] Tworzę nową pamięć bota."
        )

        return create_empty_state()

    return loaded


data = load_state()


def save_state():

    temp_file = DATA_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=4,
            ensure_ascii=False
        )

    os.replace(
        temp_file,
        DATA_FILE
    )


# ============================================================
# HTTP
# ============================================================

def fetch_page(url, expected_url=None):

    response = session.get(
        url,
        timeout=30
    )

    if response.status_code == 429:

        retry_after = response.headers.get(
            "Retry-After"
        )

        message = "HTTP 429 - za dużo zapytań"

        if retry_after:
            message += (
                f" (Retry-After: {retry_after}s)"
            )

        raise AOTYRateLimit(
            message
        )

    response.raise_for_status()

    if expected_url:

        final_url = response.url.rstrip("/").lower()
        expected = expected_url.rstrip("/").lower()

        if final_url != expected:
            raise AOTYUserNotFound()

    return response.text

def aoty_user_exists(username):

    username = username.strip()

    if not username:
        return False

    url = f"{BASE_URL}/user/{username}/"

    html = fetch_page(url)

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    title = (
        soup.title.get_text(" ", strip=True)
        if soup.title
        else ""
    )

    # Prawdziwe profile AOTY mają tytuł:
    # "<nazwa> - Profile - Album of The Year".
    # Dla nieistniejącego konta AOTY zwraca stronę główną.
    return (
        " - profile - album of the year"
        in title.casefold()
    )

def get_user_avatar(username):

    url = f"{BASE_URL}/user/{username}/"

    html = fetch_page(url)

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for img in soup.find_all("img"):

        src = (
            img.get("data-src")
            or img.get("src")
            or ""
        )

        if "/user/thumbs/" not in src:
            continue

        if src.endswith("/default.jpg"):
            continue

        if src.startswith("//"):
            src = "https:" + src

        elif src.startswith("/"):
            src = urljoin(
                BASE_URL,
                src
            )

        return src

    return None


def _find_details_row(soup, label_name):

    # Na AOTY etykiety w sekcji Details mają formę np. "/ Genre".
    # Szukamy dokładnie tej etykiety, a potem zostajemy w NAJBLIŻSZYM
    # wierszu. Dzięki temu przy braku gatunków nie wchodzimy wyżej w DOM
    # i nie łapiemy przypadkowych linków /genre/ z reszty strony.
    wanted = label_name.strip().casefold()
    exact_labels = {
        f"/ {wanted}",
        wanted,
    }

    candidates = []

    for string in soup.find_all(string=True):
        normalized = " ".join(str(string).split()).casefold()

        if normalized == f"/ {wanted}":
            candidates.insert(0, string)
        elif normalized == wanted:
            candidates.append(string)

    for label in candidates:
        container = label.parent
        best = container

        for _ in range(6):
            if not container:
                break

            labels_here = set()

            for part in container.stripped_strings:
                normalized = " ".join(str(part).split()).casefold()

                if normalized in {
                    "/ release date", "release date",
                    "/ format", "format",
                    "/ label", "label",
                    "/ genre", "genre",
                }:
                    labels_here.add(normalized.lstrip("/ "))

            # Gdy weszliśmy do kontenera zawierającego kilka pól Details,
            # jesteśmy już za wysoko — wracamy do poprzedniego poziomu.
            if len(labels_here) > 1:
                break

            if wanted in labels_here:
                best = container

            container = container.parent

        if best:
            return best

    return None


def _is_secondary_genre_link(link, row):

    node = link

    while node is not None:
        if node.name == "small":
            return True

        classes = " ".join(node.get("class", [])).casefold()
        node_id = str(node.get("id", "")).casefold()
        style = str(node.get("style", "")).replace(" ", "").casefold()

        marker = f"{classes} {node_id}"

        # AOTY może oznaczać secondary genres klasą / id.
        if any(word in marker for word in (
            "secondary",
            "secondarygenre",
            "subgenre",
            "sub-genre",
        )):
            return True

        # Dodatkowy fallback na wypadek, gdy mniejsza czcionka jest inline.
        size_match = re.search(
            r"font-size:([0-9.]+)(px|em|rem|%)",
            style
        )

        if size_match:
            value = float(size_match.group(1))
            unit = size_match.group(2)

            if (
                (unit == "px" and value <= 12)
                or (unit in {"em", "rem"} and value < 0.95)
                or (unit == "%" and value < 95)
            ):
                return True

        if node is row:
            break

        node = node.parent

    return False


def get_album_details(album_url):

    html = fetch_page(album_url)

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    year = None
    genres = []

    # ==========================
    # ROK WYDANIA
    # ==========================

    release_row = _find_details_row(
        soup,
        "release date"
    )

    if release_row:
        match = re.search(
            r"\b(?:19|20)\d{2}\b",
            release_row.get_text(" ", strip=True)
        )

        if match:
            year = match.group(0)

    # ==========================
    # TYLKO PRIMARY GENRES
    # ==========================

    genre_row = _find_details_row(
        soup,
        "genre"
    )

    if genre_row:
        passed_visual_break = False
        found_primary = False

        # Iterujemy po DOM w kolejności wyświetlania. Secondary genres na
        # AOTY są prezentowane osobno / mniejszą czcionką. Ignorujemy linki
        # oznaczone jako secondary oraz linki po pierwszym <br> po primary.
        for element in genre_row.descendants:

            if getattr(element, "name", None) == "br":
                if found_primary:
                    passed_visual_break = True
                continue

            if getattr(element, "name", None) != "a":
                continue

            href = element.get("href", "")

            if "/genre/" not in href:
                continue

            if passed_visual_break:
                continue

            if _is_secondary_genre_link(
                element,
                genre_row
            ):
                continue

            genre = element.get_text(
                " ",
                strip=True
            )

            if genre and genre not in genres:
                genres.append(genre)
                found_primary = True

    genres_text = (
        ", ".join(genres)
        if genres
        else "Brak danych"
    )

    return {
        "year": year,
        "genres": genres,
        "genres_text": genres_text
    }


# ============================================================
# TEKST
# ============================================================

def clean_text(element):

    if not element:
        return None

    text = element.get_text(
        " ",
        strip=True
    )

    if not text:
        return None

    return text


# ============================================================
# ALBUM ID
# ============================================================

def extract_album_id(href):

    if not href:
        return None

    match = re.search(
        r"/album/(\d+)",
        href
    )

    if not match:
        return None

    return match.group(1)


# ============================================================
# OCENA
# ============================================================

def extract_score(block):

    selectors = [
        ".ratingBlock .rating",
        ".rating",
        ".userRating",
        "[class*='userRating']",
    ]

    for selector in selectors:

        for element in block.select(
            selector
        ):

            text = clean_text(
                element
            )

            if not text:
                continue

            match = re.fullmatch(
                r"(100|\d{1,2})",
                text.strip()
            )

            if match:
                return match.group(1)

    return None


# ============================================================
# DATY
# ============================================================

MONTHS = {
    "jan": 1,
    "january": 1,

    "feb": 2,
    "february": 2,

    "mar": 3,
    "march": 3,

    "apr": 4,
    "april": 4,

    "may": 5,

    "jun": 6,
    "june": 6,

    "jul": 7,
    "july": 7,

    "aug": 8,
    "august": 8,

    "sep": 9,
    "sept": 9,
    "september": 9,

    "oct": 10,
    "october": 10,

    "nov": 11,
    "november": 11,

    "dec": 12,
    "december": 12,
}


def format_polish_date(text):

    if not text:
        return None

    text = text.strip()

    now = datetime.now()


    # ========================================================
    # AOTY: "just now"
    # ========================================================

    if text.lower() == "just now":

        return now.strftime(
            "%d.%m.%Y"
        )


    # ========================================================
    # AOTY: "3m ago"
    # ========================================================

    match = re.search(
        r"\b(\d+)\s*m(?:in)?\s*ago\b",
        text,
        re.IGNORECASE
    )

    if match:

        value = int(
            match.group(1)
        )

        result = (
            now
            - timedelta(
                minutes=value
            )
        )

        return result.strftime(
            "%d.%m.%Y"
        )


    # ========================================================
    # AOTY: "2h ago"
    # ========================================================

    match = re.search(
        r"\b(\d+)\s*h(?:r)?\s*ago\b",
        text,
        re.IGNORECASE
    )

    if match:

        value = int(
            match.group(1)
        )

        result = (
            now
            - timedelta(
                hours=value
            )
        )

        return result.strftime(
            "%d.%m.%Y"
        )


    # ========================================================
    # AOTY: "1d ago"
    # ========================================================

    match = re.search(
        r"\b(\d+)\s*d(?:ay)?\s*ago\b",
        text,
        re.IGNORECASE
    )

    if match:

        value = int(
            match.group(1)
        )

        result = (
            now
            - timedelta(
                days=value
            )
        )

        return result.strftime(
            "%d.%m.%Y"
        )


    # ========================================================
    # AOTY: "1w ago"
    # ========================================================

    match = re.search(
        r"\b(\d+)\s*w(?:eek)?\s*ago\b",
        text,
        re.IGNORECASE
    )

    if match:

        value = int(
            match.group(1)
        )

        result = (
            now
            - timedelta(
                weeks=value
            )
        )

        return result.strftime(
            "%d.%m.%Y"
        )


    # ========================================================
    # Aug 15 / Aug 15, 2026
    # ========================================================

    match = re.search(
        r"\b("
        r"Jan(?:uary)?|"
        r"Feb(?:ruary)?|"
        r"Mar(?:ch)?|"
        r"Apr(?:il)?|"
        r"May|"
        r"Jun(?:e)?|"
        r"Jul(?:y)?|"
        r"Aug(?:ust)?|"
        r"Sep(?:t(?:ember)?|tember)?|"
        r"Oct(?:ober)?|"
        r"Nov(?:ember)?|"
        r"Dec(?:ember)?"
        r")\s+"
        r"(\d{1,2})"
        r"(?:,\s*(\d{4}))?",
        text,
        re.IGNORECASE
    )

    if not match:
        return None

    month_name = (
        match
        .group(1)
        .lower()
    )

    day = int(
        match.group(2)
    )

    month = MONTHS.get(
        month_name
    )

    if not month:
        return None

    year_text = match.group(3)

    if year_text:

        year = int(
            year_text
        )

    else:

        year = now.year

        try:

            candidate = datetime(
                year,
                month,
                day
            )

            # np. Dec 29 widziane w styczniu
            # = prawdopodobnie poprzedni rok
            if (
                candidate
                > now + timedelta(days=2)
            ):
                year -= 1

        except ValueError:
            return None

    try:

        result = datetime(
            year,
            month,
            day
        )

    except ValueError:
        return None

    return result.strftime(
        "%d.%m.%Y"
    )


def extract_date(block):

    # ========================================================
    # NAJPIERW TYPOWE KLASY
    # ========================================================

    selectors = [
        ".ratingText",
        ".date",
        "[class*='date']",
        "[class*='Date']",
    ]

    for selector in selectors:

        for element in block.select(
            selector
        ):

            text = clean_text(
                element
            )

            if not text:
                continue

            date = format_polish_date(
                text
            )

            if date:
                return date


    # ========================================================
    # POTEM KAŻDY FRAGMENT TEKSTU
    # ========================================================

    for text in block.stripped_strings:

        date = format_polish_date(
            text
        )

        if date:
            return date


    return "Brak danych"


# ============================================================
# OKŁADKA
# ============================================================

def extract_cover(block):

    image = block.select_one(
        "img"
    )

    if not image:
        return None

    cover = (
        image.get("data-src")
        or image.get("data-lazy-src")
        or image.get("src")
    )

    if not cover:
        return None

    if cover.startswith("//"):

        cover = (
            "https:"
            + cover
        )

    elif cover.startswith("/"):

        cover = urljoin(
            BASE_URL,
            cover
        )

    return cover


# ============================================================
# PARSER POJEDYNCZEGO ALBUMU
# ============================================================

def parse_album_block(block):

    album_title_element = (
        block.select_one(
            ".albumTitle"
        )
    )

    album_link = None


    # ========================================================
    # LINK ALBUMU
    # ========================================================

    if album_title_element:

        if (
            album_title_element.name == "a"
            and album_title_element.get(
                "href"
            )
        ):

            album_link = (
                album_title_element
            )

        else:

            album_link = (
                album_title_element
                .select_one(
                    'a[href*="/album/"]'
                )
            )


    # Fallback
    if not album_link:

        links = block.select(
            'a[href*="/album/"]'
        )

        for link in links:

            if clean_text(
                link
            ):

                album_link = link
                break

        if (
            not album_link
            and links
        ):

            album_link = (
                links[0]
            )


    if not album_link:
        return None


    href = album_link.get(
        "href",
        ""
    )


    album_id = extract_album_id(
        href
    )

    if not album_id:
        return None


    # ========================================================
    # ALBUM
    # ========================================================

    album = None

    if album_title_element:

        album = clean_text(
            album_title_element
        )

    if not album:

        album = clean_text(
            album_link
        )


    # ========================================================
    # ARTYSTA
    # ========================================================

    artist = None

    artist_element = (
        block.select_one(
            ".artistTitle"
        )
    )

    if artist_element:

        artist = clean_text(
            artist_element
        )


    if not artist:

        artist_link = (
            block.select_one(
                'a[href*="/artist/"]'
            )
        )

        artist = clean_text(
            artist_link
        )


    # ========================================================
    # OCENA
    # ========================================================

    score = extract_score(
        block
    )

    if not score:
        return None


    # ========================================================
    # DATA
    # ========================================================

    date = extract_date(
        block
    )

    if date == "Brak danych":
        date = datetime.now().strftime("%d.%m.%Y")


    # ========================================================
    # OKŁADKA
    # ========================================================

    cover = extract_cover(
        block
    )


    # ========================================================
    # URL
    # ========================================================

    album_url = urljoin(
        BASE_URL,
        href
    )


    if not artist:
        artist = "Nieznany artysta"

    if not album:
        album = (
            f"Album #{album_id}"
        )


    return {
        "album_id": album_id,
        "artist": artist,
        "album": album,
        "score": score,
        "date": date,
        "url": album_url,
        "cover": cover
    }


# ============================================================
# FALLBACK PARSER
# ============================================================

def parse_generic(soup):

    results = {}

    links = soup.select(
        'a[href*="/album/"]'
    )


    for link in links:

        href = link.get(
            "href",
            ""
        )

        album_id = extract_album_id(
            href
        )

        if not album_id:
            continue

        if album_id in results:
            continue


        container = link


        # Maksymalnie 10 poziomów DOM.
        for _ in range(10):

            container = (
                container.parent
            )

            if not container:
                break


            # Sprawdzamy, ile różnych albumów
            # zawiera kontener.
            album_ids = set()

            for album_link in (
                container.select(
                    'a[href*="/album/"]'
                )
            ):

                found_id = (
                    extract_album_id(
                        album_link.get(
                            "href",
                            ""
                        )
                    )
                )

                if found_id:

                    album_ids.add(
                        found_id
                    )


            # Jeśli weszliśmy za wysoko w DOM,
            # kontener zawiera już kilka albumów.
            if len(album_ids) > 1:
                break


            score = extract_score(
                container
            )

            if not score:
                continue


            item = parse_album_block(
                container
            )


            if (
                item
                and item["album_id"]
                == album_id
            ):

                results[
                    album_id
                ] = item

                break


    return list(
        results.values()
    )

# ============================================================
# POBIERANIE OCEN
# ============================================================

def get_ratings(username, max_pages=3):

    all_ratings = []
    seen = set()

    for page in range(1, max_pages + 1):

        if page == 1:
            url = f"{BASE_URL}/user/{username}/ratings/"
        else:
            url = f"{BASE_URL}/user/{username}/ratings/{page}/"

        html = fetch_page(url)

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        results = {}

        for block in soup.select(".albumBlock"):

            item = parse_album_block(block)

            if not item:
                continue

            results[item["album_id"]] = item

        for item in parse_generic(soup):

            album_id = item["album_id"]

            if album_id not in results:
                results[album_id] = item

        page_ratings = []

        added = set()

        for link in soup.select('a[href*="/album/"]'):

            album_id = extract_album_id(
                link.get("href", "")
            )

            if not album_id:
                continue

            if album_id not in results:
                continue

            if album_id in added:
                continue

            page_ratings.append(
                results[album_id]
            )

            added.add(album_id)

        if not page_ratings:
            break

        for item in page_ratings:

            album_id = item["album_id"]

            if album_id in seen:
                continue

            seen.add(album_id)

            all_ratings.append(item)

    return all_ratings


# ============================================================
# KOLORY
# ============================================================

def score_color(score):

    try:

        score = int(
            score
        )

    except (
        TypeError,
        ValueError
    ):

        return (
            discord.Color
            .light_grey()
        )


    if score == 100:

        return (
            discord.Color
            .from_rgb(
                66,
                255,
                255
            )
        )


    if score >= 90:

        return (
            discord.Color
            .from_rgb(
                28,
                242,
                155
            )
        )


    if score >= 80:

        return (
            discord.Color
            .from_rgb(
                18,
                215,
                98
            )
        )


    if score >= 70:

        return (
            discord.Color
            .from_rgb(
                51,
                255,
                0
            )
        )


    if score >= 60:

        return (
            discord.Color
            .from_rgb(
                174,
                255,
                0
            )
        )


    if score >= 50:

        return (
            discord.Color
            .from_rgb(
                255,
                229,
                0
            )
        )


    if score >= 40:

        return (
            discord.Color
            .from_rgb(
                255,
                157,
                0
            )
        )


    if score >= 30:

        return (
            discord.Color
            .from_rgb(
                255,
                91,
                0
            )
        )


    if score >= 20:

        return (
            discord.Color
            .from_rgb(
                255,
                31,
                15
            )
        )


    if score >= 10:

        return (
            discord.Color
            .from_rgb(
                140,
                20,
                20
            )
        )


    return (
        discord.Color
        .from_rgb(
            58,
            10,
            10
        )
    )


# ============================================================
# IKONA OCENY
# ============================================================

def score_icon(score):

    try:
        score = int(score)

    except (
        TypeError,
        ValueError
    ):
        return "⚪"


    if score == 100:
        return "💎"

    if score >= 90:
        return "💚"

    if score >= 80:
        return "🟢"

    if score >= 70:
        return "🟢"

    if score >= 60:
        return "🟡"
    
    if score >= 50:
        return "🟡"

    if score >= 40:
        return "🟠"

    if score >= 30:
        return "🟠"

    if score >= 20:
        return "🔴"
            
    if score >= 10:
        return "❓"

    return "⚫"


# ============================================================
# DISCORD
# ============================================================


async def get_discord_channel(username):

    channel_id = USER_CHANNELS.get(
        username.lower(),
        CHANNEL_ID
    )

    channel = client.get_channel(
        channel_id
    )

    if channel:
        return channel

    try:
        return (
            await client.fetch_channel(
                channel_id
            )
        )

    except Exception as e:
        print(
            "[DISCORD] "
            f"Nie znaleziono kanału dla {username} "
            f"({channel_id}): {e}"
        )
        return None


# ============================================================
# NOWA OCENA — EMBED
# ============================================================

async def send_new_rating(username, item, avatar=None):

    channel = await get_discord_channel(username)

    if channel is None:
        return False

    score = item["score"]
    artist = item["artist"]
    album = item["album"]
    date = item["date"]
    url = item["url"]
    cover = item["cover"]

    # Dodatkowe dane albumu do użycia w embedzie.
    # Pobieramy je dopiero przy faktycznie wysyłanej aktualizacji.
    year = "Brak danych"
    genres = []
    genres_text = "Brak danych"
    main_genre = "Brak danych"
    other_genres = "Brak danych"
    other_genres_text = "Brak danych"

    try:
        details = await asyncio.to_thread(
            get_album_details,
            url
        )

        year = details.get("year") or "Brak danych"
        genres = details.get("genres") or []
        genres_text = details.get("genres_text") or "Brak danych"

        if genres:
            main_genre = genres[0]

            if len(genres) > 1:
                other_genres = ", ".join(genres[1:])
                other_genres_text = other_genres

    except Exception as e:
        print(
            f"[AOTY] Nie udało się pobrać szczegółów albumu "
            f"{artist} — {album}: {type(e).__name__}: {e}"
        )

    embed = discord.Embed(
        title=f"{album}",
        url=url,
        description=f"**{artist}**",
        color=score_color(score)
    )

    embed.add_field(
        name=f"**{score}**  {score_icon(score)}",
        value=" ",
        inline=True
    )

    if cover:
        embed.set_thumbnail(
            url=cover
        )

    if avatar:
        embed.set_footer(
            text=f"{username} AOTY  •  {date}  ⚠️",
            icon_url=avatar
        )
    else:
        embed.set_footer(
            text=f"{username} AOTY  •  {date}  ⚠️"
        )

    try:
        await channel.send(
            embed=embed
        )

        print(
            f"[DISCORD] Wysłano: "
            f"{artist} — {album} ({score}/100)"
        )

        return True

    except Exception as e:
        print(
            f"[DISCORD] Błąd wysyłania: "
            f"{type(e).__name__}: {e}"
        )

        return False


# ============================================================
# ZMIANA OCENY — EMBED
# ============================================================

async def send_changed_rating(
    username,
    item,
    old_score,
    avatar=None
):

    channel = await get_discord_channel(username)

    if channel is None:
        return False

    new_score = item["score"]
    artist = item["artist"]
    album = item["album"]
    date = item["date"]
    url = item["url"]
    cover = item["cover"]

    # Dodatkowe dane albumu do użycia w embedzie.
    # Pobieramy je dopiero przy faktycznie wysyłanej zmianie oceny.
    year = "Brak danych"
    genres = []
    genres_text = "Brak danych"
    main_genre = "Brak danych"
    other_genres = "Brak danych"
    other_genres_text = "Brak danych"

    try:
        details = await asyncio.to_thread(
            get_album_details,
            url
        )

        year = details.get("year") or "Brak danych"
        genres = details.get("genres") or []
        genres_text = details.get("genres_text") or "Brak danych"

        if genres:
            main_genre = genres[0]

            if len(genres) > 1:
                other_genres = ", ".join(genres[1:])
                other_genres_text = other_genres

    except Exception as e:
        print(
            f"[AOTY] Nie udało się pobrać szczegółów albumu "
            f"{artist} — {album}: {type(e).__name__}: {e}"
        )

    embed = discord.Embed(
        title=f"{album}",
        url=url,
        description=f"{artist}",
        color=score_color(new_score)
    )

    embed.add_field(
        name=f"*{old_score}*  ➞  **{new_score}**  {score_icon(new_score)}",
        value=f" ",
        inline=True
    )

    if cover:
        embed.set_thumbnail(
            url=cover
        )

    if avatar:
        embed.set_footer(
            text=f"{username} AOTY  •  {date}  🔄",
            icon_url=avatar
        )
    else:
        embed.set_footer(
            text=f"{username} AOTY  •  {date}  🔄"
        )

    try:
        await channel.send(
            embed=embed
        )

        print(
            f"[DISCORD] Wysłano zmianę: "
            f"{artist} — {album} "
            f"{old_score} → {new_score}"
        )

        return True

    except Exception as e:
        print(
            f"[DISCORD] Błąd wysyłania: "
            f"{type(e).__name__}: {e}"
        )

        return False


# ============================================================
# SPRAWDZANIE PROFILU
# ============================================================

async def check_user(
    username
):

    print(
        f"[AOTY] Sprawdzam {username}..."
    )


    try:

        ratings = (
            await asyncio.to_thread(
                get_ratings,
                username
            )
        )

    except AOTYRateLimit as e:

        print(
            f"[AOTY] {username}: {e}"
        )

        return

    except requests.RequestException as e:

        print(
            f"[AOTY] {username}: "
            f"błąd HTTP: {e}"
        )

        return

    except Exception as e:

        print(
            f"[AOTY] {username}: "
            f"{type(e).__name__}: {e}"
        )

        return


    # Avatar jest dodatkiem wizualnym. Jego brak nie może zatrzymać
    # wykrywania ani zapisywania nowych ocen.
    avatar = None

    try:
        avatar = await asyncio.to_thread(
            get_user_avatar,
            username
        )
    except Exception as e:
        print(
            f"[AOTY] {username}: nie udało się pobrać avatara: "
            f"{type(e).__name__}: {e}"
        )


    if not ratings:

        print(
            f"[AOTY] {username}: "
            "nie znaleziono ocen."
        )

        return


    print(
        f"[AOTY] {username}: "
        f"znaleziono {len(ratings)} ocen."
    )


    users_data = data[
        "users"
    ]


    # ========================================================
    # PIERWSZE URUCHOMIENIE
    # ========================================================

    if username not in users_data:

        users_data[
            username
        ] = {
            "ratings": {}
        }

        known = (
            users_data[
                username
            ][
                "ratings"
            ]
        )


        for item in ratings:

            known[
                item["album_id"]
            ] = {

                "score": item[
                    "score"
                ],

                "date": item[
                    "date"
                ],

                "artist": item[
                    "artist"
                ],

                "album": item[
                    "album"
                ]
            }


        save_state()


        print(
            f"[AOTY] {username}: "
            "pierwsze uruchomienie — "
            "zapamiętuję aktualny stan."
        )

        return


    known = (
        users_data[
            username
        ].setdefault(
            "ratings",
            {}
        )
    )


    # ========================================================
    # KLUCZOWA ZMIANA:
    #
    # NAJPIERW wyliczamy WSZYSTKIE nowe oceny.
    # Dopiero potem cokolwiek zapisujemy.
    # ========================================================

    new_items = []

    changed_items = []


    for item in ratings:

        album_id = item[
            "album_id"
        ]


        previous = known.get(
            album_id
        )


        # Nowy album
        if previous is None:

            new_items.append(
                item
            )

            continue


        old_score = str(
            previous.get(
                "score",
                ""
            )
        )

        new_score = str(
            item[
                "score"
            ]
        )


        # Zmieniona ocena
        if old_score != new_score:

            changed_items.append(
                (
                    item,
                    old_score
                )
            )


    # ========================================================
    # RAPORT
    # ========================================================

    print(
        f"[AOTY] {username}: "
        f"nowych={len(new_items)}, "
        f"zmienionych={len(changed_items)}"
    )


    # ========================================================
    # NOWE OCENY
    #
    # ratings na AOTY są od najnowszych,
    # więc odwracamy tylko listę nowych.
    # ========================================================

    for item in reversed(
        new_items
    ):

        print(
            f"[AOTY] NOWA: "
            f"{item['artist']} — "
            f"{item['album']} "
            f"{item['score']}/100 "
            f"({item['date']})"
        )


        sent = await send_new_rating(
            username,
            item,
            avatar
        )


        if sent:

            known[
                item["album_id"]
            ] = {

                "score": item[
                    "score"
                ],

                "date": item[
                    "date"
                ],

                "artist": item[
                    "artist"
                ],

                "album": item[
                    "album"
                ]
            }


            # Zapis po KAŻDEJ poprawnie
            # wysłanej wiadomości.
            save_state()


            # Mały odstęp pomiędzy wiadomościami.
            await asyncio.sleep(
                1
            )


    # ========================================================
    # ZMIANY OCEN
    # ========================================================

    for (
        item,
        old_score
    ) in reversed(
        changed_items
    ):

        print(
            f"[AOTY] ZMIANA: "
            f"{item['artist']} — "
            f"{item['album']} "
            f"{old_score} -> "
            f"{item['score']}"
        )


        sent = (
            await send_changed_rating(
                username,
                item,
                old_score,
                avatar
            )
        )


        if sent:

            known[
                item["album_id"]
            ] = {

                "score": item[
                    "score"
                ],

                "date": item[
                    "date"
                ],

                "artist": item[
                    "artist"
                ],

                "album": item[
                    "album"
                ]
            }


            save_state()


            await asyncio.sleep(
                1
            )


    # ========================================================
    # AKTUALIZACJA METADANYCH STARYCH OCEN
    # ========================================================

    for item in ratings:

        album_id = item[
            "album_id"
        ]

        if album_id not in known:
            continue


        # Nie nadpisujemy score tutaj,
        # jeśli wiadomości nie udało się wysłać.
        current_score = str(
            known[
                album_id
            ].get(
                "score",
                ""
            )
        )


        if current_score == str(
            item[
                "score"
            ]
        ):

            known[
                album_id
            ][
                "date"
            ] = item[
                "date"
            ]

            known[
                album_id
            ][
                "artist"
            ] = item[
                "artist"
            ]

            known[
                album_id
            ][
                "album"
            ] = item[
                "album"
            ]


    save_state()


# ============================================================
# MONITOR
# ============================================================

async def monitor():

    await client.wait_until_ready()


    print()
    print("==============================")
    print("        KOTONE")
    print("==============================")

    print(
        "Monitoruję:",
        ", ".join(
            USERS
        )
    )

    print(
        "Interwał:",
        CHECK_INTERVAL,
        "sekund"
    )

    print("STATUS:", client.status)
    print("ACTIVITY:", client.activity)
    print("TYPE:", client.activity.type if client.activity else None)
    print(client.activity.type)

    print("==============================")
    print()


    while not client.is_closed():

        for username in USERS:

            await check_user(
                username
            )

            await asyncio.sleep(
                2
            )


        print(
            f"[BOT] Następne sprawdzenie "
            f"za {CHECK_INTERVAL} sekund."
        )


        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# DISCORD READY
# ============================================================

monitor_task = None

setup_last_command(
    tree=tree,
    get_ratings=get_ratings,
    get_user_avatar=get_user_avatar,
    get_album_details=get_album_details,
    aoty_user_exists=aoty_user_exists,
    score_color=score_color,
    score_icon=score_icon,
    AOTYRateLimit=AOTYRateLimit,
)

async def setup_hook():

    guild = discord.Object(
        id=GUILD_ID
    )

    tree.copy_global_to(
        guild=guild
    )

    synced = await tree.sync(
        guild=guild
    )

# Usuwamy stare globalne komendy,
# żeby Discord nie pokazywał ich podwójnie
    tree.clear_commands(
        guild=None
    )

    await tree.sync()

    print(
        f"[DISCORD] Zsynchronizowano "
        f"{len(synced)} komend na serwerze."
    )

    for command in synced:
        print(
            f"[DISCORD] /{command.name}"
        )


client.setup_hook = setup_hook

@client.event
async def on_ready():

    global monitor_task


    print(
        f"Zalogowano jako {client.user}"
    )

    if (
        monitor_task is None
        or monitor_task.done()
    ):

        monitor_task = (
            asyncio.create_task(
                monitor()
            )
        )


# ============================================================
# START
# ============================================================

client.run(
    TOKEN
)