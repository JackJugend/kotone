"""Synchronizacja emoji serwerowych z avatarami AOTY zapisanymi w SQLite.

Discord nie pozwala zmienić pliku istniejącego custom emoji.  Dlatego przy
zmianie avatara tworzymy najpierw tymczasowe emoji, dopiero potem usuwamy
poprzednie botowe emoji i zmieniamy nazwę nowego na stałą (np. ``enso``).
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import time
from urllib.parse import urlparse

import discord
import requests
from PIL import Image, ImageDraw, ImageOps

from database import DB
from http_client import HTTP
from settings import GUILD_ID, KOTONE_AVATAR_EMOJI_NAMES

MAX_EMOJI_BYTES = 256 * 1024
MAX_SOURCE_AVATAR_BYTES = 4 * 1024 * 1024
EMOJI_AVATAR_SIZE = 128


def _safe_aoty_avatar_bytes(url: str) -> bytes:
    """Download, crop and encode a round AOTY avatar for Discord."""

    parsed = urlparse(str(url or "").strip())
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith("albumoftheyear.org"):
        raise ValueError("avatar AOTY ma niedozwolony URL")

    response = requests.get(url, timeout=(5, 20))
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
    """Replace only bot-owned cached avatar emojis when their source changes."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._lock = asyncio.Lock()

    async def sync_cached(self) -> None:
        """Create missing emojis from existing SQLite avatars without AOTY IO."""

        if HTTP.status().get("challenge_open"):
            print("[AVATAR EMOJI] Pominięto sync: aktywny cooldown AOTY.")
            return
        for username in KOTONE_AVATAR_EMOJI_NAMES:
            await self.sync_user(username)

    async def sync_user(self, username: str) -> bool:
        """Synchronize one configured AOTY user's emoji from SQLite only."""

        canonical = DB.canonical_username(username)
        if canonical is None:
            return False
        if HTTP.status().get("challenge_open"):
            return False
        emoji_name = KOTONE_AVATAR_EMOJI_NAMES.get(canonical.casefold())
        avatar_url = DB.get_avatar(canonical)
        if not emoji_name or not avatar_url:
            return False

        async with self._lock:
            try:
                return await self._sync_user_locked(canonical, emoji_name, avatar_url)
            except Exception as exc:
                DB.mark_avatar_emoji_error(canonical, f"{type(exc).__name__}: {exc}")
                print(f"[AVATAR EMOJI] {canonical}: {type(exc).__name__}: {exc}")
                return False

    async def _sync_user_locked(
        self,
        username: str,
        emoji_name: str,
        avatar_url: str,
    ) -> bool:
        guild = self.client.get_guild(GUILD_ID)
        if guild is None:
            raise RuntimeError("serwer Discord nie jest jeszcze w cache klienta")

        state = DB.get_avatar_emoji_state(username) or {}
        emoji_id = str(state.get("emoji_id") or "")
        existing = next(
            (emoji for emoji in guild.emojis if str(emoji.id) == emoji_id),
            None,
        )
        if existing is not None and state.get("avatar_url") == avatar_url:
            return False

        # Never replace a same-named emoji that Kotone did not create and has
        # not recorded in SQLite. It could be a manually uploaded server asset.
        name_owner = next(
            (emoji for emoji in guild.emojis if emoji.name == emoji_name),
            None,
        )
        if existing is None and name_owner is not None and not emoji_id:
            raise RuntimeError(
                f"emoji :{emoji_name}: już istnieje; Kotone nie nadpisze ręcznego emoji"
            )

        image = await asyncio.to_thread(_safe_aoty_avatar_bytes, avatar_url)
        if existing is None:
            created = await guild.create_custom_emoji(
                name=emoji_name,
                image=image,
                reason=f"Kotone: avatar AOTY użytkownika {username}",
            )
        else:
            temporary_name = f"k_{emoji_name}_{int(time.time()) % 1000000}"
            created = await guild.create_custom_emoji(
                name=temporary_name[:32],
                image=image,
                reason=f"Kotone: nowy avatar AOTY użytkownika {username}",
            )
            await existing.delete(
                reason=f"Kotone: zastąpiono avatar AOTY użytkownika {username}",
            )
            await created.edit(
                name=emoji_name,
                reason=f"Kotone: przywrócono stałą nazwę emoji {emoji_name}",
            )

        DB.save_avatar_emoji_state(username, created.id, avatar_url)
        print(f"[AVATAR EMOJI] {username}: zapisano :{emoji_name}:")
        return True
