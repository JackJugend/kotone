"""Synchronizacja emoji aplikacji z avatarami AOTY zapisanymi w SQLite.

Discord nie pozwala zmienić pliku istniejącego custom emoji.  Dlatego przy
zmianie avatara tworzymy najpierw tymczasowe emoji, dopiero potem usuwamy
poprzednie botowe emoji i zmieniamy nazwę nowego na stałą (np. ``enso``).

Używamy Application Emojis, nie emoji serwera: są własnością Kotone i nie
zaśmiecają listy emoji na żadnym guildzie, a bot może ich używać we własnych
wiadomościach niezależnie od uprawnień do emoji na serwerze.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import time
from urllib.parse import urlparse

import discord
import requests
from PIL import Image, ImageDraw, ImageOps

from database import DB
from settings import KOTONE_AVATAR_EMOJI_NAMES

MAX_EMOJI_BYTES = 256 * 1024
MAX_SOURCE_AVATAR_BYTES = 4 * 1024 * 1024
EMOJI_AVATAR_SIZE = 128

# AOTY's CDN sometimes refuses a bare Python request.  These are ordinary
# browser fetch headers; they do not bypass a challenge or a cooldown.
AVATAR_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.albumoftheyear.org/",
}


def _safe_avatar_bytes(url: str) -> bytes:
    """Download, crop and encode an approved avatar image for Discord."""

    parsed = urlparse(str(url or "").strip())
    host = parsed.hostname or ""
    allowed_hosts = (
        host.endswith("albumoftheyear.org")
        or host in {"cdn.discordapp.com", "media.discordapp.net"}
    )
    if parsed.scheme != "https" or not allowed_hosts:
        raise ValueError("avatar ma niedozwolony URL")

    response = requests.get(url, headers=AVATAR_REQUEST_HEADERS, timeout=(5, 20))
    response.raise_for_status()
    data = response.content
    if not data or len(data) > MAX_SOURCE_AVATAR_BYTES:
        raise ValueError("źródłowy avatar jest pusty albo zbyt duży")

    with Image.open(BytesIO(data)) as source:
        # Discord emoji supports transparency, so the corners stay genuinely
        # transparent instead of being filled with an arbitrary background.
        image = ImageOps.fit(
            source.convert("RGBA"),
            (EMOJI_AVATAR_SIZE, EMOJI_AVATAR_SIZE),
            method=Image.Resampling.LANCZOS,
        )
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, *image.size), fill=255)
    image.putalpha(mask)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    encoded = output.getvalue()
    if len(encoded) > MAX_EMOJI_BYTES:
        raise ValueError("okrągły avatar przekracza limit 256 KiB emoji Discorda")
    return encoded


class AvatarEmojiSynchronizer:
    """Replace app-owned cached avatar emojis when their source changes."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._lock = asyncio.Lock()
        self._application_id: int | None = None

    async def _application_route(self, method: str, path: str, **parameters):
        """Build a Discord application-emoji route using discord.py's auth.

        discord.py 2.7 does not yet expose high-level helpers for application
        emojis, but its authenticated HTTP client is stable and prevents us
        from storing or manually handling the bot token.
        """

        if self._application_id is None:
            application_id = getattr(self.client, "application_id", None)
            if application_id is None:
                info = await self.client.application_info()
                application_id = info.id
            self._application_id = int(application_id)
        return discord.http.Route(
            method,
            path,
            application_id=self._application_id,
            **parameters,
        )

    async def _list_application_emojis(self) -> list[dict]:
        route = await self._application_route(
            "GET",
            "/applications/{application_id}/emojis",
        )
        payload = await self.client.http.request(route)
        return list((payload or {}).get("items") or [])

    @staticmethod
    def _image_data_uri(image: bytes) -> str:
        encoded = base64.b64encode(image).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    async def sync_cached(self) -> None:
        """Create missing emojis from existing SQLite avatars without AOTY IO."""

        for username in KOTONE_AVATAR_EMOJI_NAMES:
            await self.sync_user(username)

    async def sync_user(self, username: str) -> bool:
        """Synchronize one configured user's avatar using its SQLite cache."""

        canonical = DB.canonical_username(username)
        if canonical is None:
            return False
        emoji_name = KOTONE_AVATAR_EMOJI_NAMES.get(canonical.casefold())
        avatar_url = DB.get_avatar(canonical)
        if not emoji_name or not avatar_url:
            return False

        async with self._lock:
            try:
                state = DB.get_avatar_emoji_state(canonical) or {}
                image = DB.get_avatar_image(canonical, avatar_url)
                if image is None:
                    # If an older application emoji exists, cache its pixels
                    # from Discord first.  That repairs the local cache even
                    # while AOTY is challenging our IP.
                    old_emoji_id = str(state.get("emoji_id") or "").strip()
                    if old_emoji_id and state.get("avatar_url") == avatar_url:
                        discord_url = (
                            f"https://cdn.discordapp.com/emojis/{old_emoji_id}.png"
                            "?size=128&quality=lossless"
                        )
                        try:
                            image = await asyncio.to_thread(_safe_avatar_bytes, discord_url)
                        except Exception:
                            image = None
                    if image is None:
                        image = await asyncio.to_thread(_safe_avatar_bytes, avatar_url)
                    DB.save_avatar_image(canonical, avatar_url, image)
                return await self._sync_user_locked(
                    canonical,
                    emoji_name,
                    avatar_url,
                    image,
                )
            except Exception as exc:
                DB.mark_avatar_emoji_error(canonical, f"{type(exc).__name__}: {exc}")
                print(f"[AVATAR EMOJI] {canonical}: {type(exc).__name__}: {exc}")
                return False

    async def _sync_user_locked(
        self,
        username: str,
        emoji_name: str,
        avatar_url: str,
        image: bytes,
    ) -> bool:
        state = DB.get_avatar_emoji_state(username) or {}
        emoji_id = str(state.get("emoji_id") or "")
        application_emojis = await self._list_application_emojis()
        existing = next(
            (emoji for emoji in application_emojis if str(emoji.get("id")) == emoji_id),
            None,
        )
        if existing is not None and state.get("avatar_url") == avatar_url:
            return False

        # Never replace a same-named emoji which is not recorded in SQLite.
        # It may have been uploaded manually in the Discord Developer Portal.
        name_owner = next(
            (emoji for emoji in application_emojis if emoji.get("name") == emoji_name),
            None,
        )
        if existing is None and name_owner is not None and not emoji_id:
            raise RuntimeError(
                f"emoji :{emoji_name}: już istnieje; Kotone nie nadpisze ręcznego emoji"
            )

        create_route = await self._application_route(
            "POST",
            "/applications/{application_id}/emojis",
        )
        if existing is None:
            created = await self.client.http.request(
                create_route,
                json={
                    "name": emoji_name,
                    "image": self._image_data_uri(image),
                },
            )
        else:
            temporary_name = f"k_{emoji_name}_{int(time.time()) % 1000000}"
            created = await self.client.http.request(
                create_route,
                json={
                    "name": temporary_name[:32],
                    "image": self._image_data_uri(image),
                },
            )
            delete_route = await self._application_route(
                "DELETE",
                "/applications/{application_id}/emojis/{emoji_id}",
                emoji_id=existing["id"],
            )
            await self.client.http.request(delete_route)
            rename_route = await self._application_route(
                "PATCH",
                "/applications/{application_id}/emojis/{emoji_id}",
                emoji_id=created["id"],
            )
            created = await self.client.http.request(
                rename_route,
                json={"name": emoji_name},
            )

        DB.save_avatar_emoji_state(username, created["id"], avatar_url)
        print(f"[AVATAR EMOJI] {username}: zapisano :{emoji_name}:")
        return True
