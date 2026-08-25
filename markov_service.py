"""Trwały moduł rozmów Kotone oparty na łańcuchu Markowa.

Silnik zachowuje publiczną ideę ``read``/``generate_text`` z projektu
``esdalmaijer/markovbot`` (GPL-3.0):
https://github.com/esdalmaijer/markovbot

Oryginał powstał dla Pythona 3.5 i Twittera. Ta integracja implementuje sam
łańcuch drugiego rzędu w sposób zgodny z Pythonem 3.13, a korpus zapisuje w
SQLite na Railway Volume. Nie przechowujemy wiadomości botów ani pingów Kotone.
Pingi innych użytkowników pozostają zwykłymi tokenami ``<@id>`` i mogą pojawić
się w wygenerowanej odpowiedzi.
"""

from __future__ import annotations

import asyncio
import random
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from typing import TYPE_CHECKING

from config_core import CONFIG, MARKOV_DATABASE_FILE
from settings import GUILD_ID

if TYPE_CHECKING:
    import discord


_END = "\0KOTONE_MARKOV_END"
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MASS_MENTION_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
_WHITESPACE_RE = re.compile(r"\s+")
_COMPARISON_TOKEN_RE = re.compile(r"<@!?\d+>|\w+", re.UNICODE)


def _comparison_key(text: object) -> str:
    """Ujednolić tekst do wykrywania kopii mimo interpunkcji i wielkości."""

    return " ".join(_COMPARISON_TOKEN_RE.findall(str(text or "").casefold()))


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


MARKOV_CONFIG = dict(CONFIG.get("markov") or {})
MARKOV_CHANNEL_ID = _integer(MARKOV_CONFIG.get("channel_id"), 1021030274424897629)
MARKOV_DEFAULT_ENABLED = bool(MARKOV_CONFIG.get("enabled", True))
MARKOV_HISTORY_LIMIT = max(1, _integer(MARKOV_CONFIG.get("history_limit"), 1000))
MARKOV_SPONTANEOUS_EVERY = max(
    1,
    _integer(MARKOV_CONFIG.get("spontaneous_every"), 15),
)
MARKOV_MAX_WORDS = max(5, _integer(MARKOV_CONFIG.get("max_words"), 35))


class KotoneMarkovBot:
    """Niewielki łańcuch drugiego rzędu inspirowany ``MarkovBot``.

    Następne słowo jest losowane z listy zawierającej powtórzenia. Dzięki temu
    częściej występujące przejścia mają dokładnie taką samą przewagę wagową jak
    w oryginalnym projekcie.
    """

    def __init__(self) -> None:
        self._transitions: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._starts: list[tuple[str, str]] = []
        self._token_count = 0
        self._lock = threading.RLock()

    def clear_data(self) -> None:
        with self._lock:
            self._transitions.clear()
            self._starts.clear()
            self._token_count = 0

    def read_text(self, text: object) -> int:
        """Dodaj jedną wiadomość do modelu i zwróć liczbę tokenów."""

        words = str(text or "").split()
        if len(words) < 2:
            return len(words)
        with self._lock:
            self._starts.append((words[0], words[1]))
            padded = [*words, _END]
            for index in range(len(padded) - 2):
                key = (padded[index], padded[index + 1])
                self._transitions[key].append(padded[index + 2])
            self._token_count += len(words)
        return len(words)

    def read_many(self, texts: Iterable[object]) -> int:
        return sum(self.read_text(text) for text in texts)

    def generate_text(
        self,
        maxlength: int,
        seedword: str | Iterable[str] | None = None,
    ) -> str:
        """Wygeneruj tekst o długości najwyżej ``maxlength`` słów."""

        with self._lock:
            if not self._transitions or not self._starts:
                return ""
            transitions = {
                key: tuple(values)
                for key, values in self._transitions.items()
            }
            starts = tuple(self._starts)

        if isinstance(seedword, str):
            seeds = seedword.split()
        else:
            seeds = [str(value) for value in (seedword or []) if str(value)]
        lowered = {seed.casefold() for seed in seeds}
        matching = [
            key
            for key in transitions
            if lowered.intersection({key[0].casefold(), key[1].casefold()})
        ]
        first, second = random.choice(matching or list(starts))
        words = [first, second]
        seen_states: dict[tuple[str, str], int] = defaultdict(int)

        while len(words) < max(2, int(maxlength)):
            state = (words[-2], words[-1])
            choices = transitions.get(state)
            if not choices:
                break
            seen_states[state] += 1
            # Mały korpus potrafi tworzyć pętle. Dwa powroty do tego samego
            # stanu wystarczą, by zakończyć zdanie bez blokowania event loopa.
            if seen_states[state] > 2:
                break
            next_word = random.choice(choices)
            if next_word == _END:
                break
            words.append(next_word)

        return " ".join(words).strip()

    def stats(self) -> dict[str, int]:
        with self._lock:
            unique_words = {
                word
                for pair in self._transitions
                for word in pair
                if word != _END
            }
            return {
                "tokens": self._token_count,
                "words": len(unique_words),
                "transitions": len(self._transitions),
            }


class MarkovStore:
    """Trwały korpus i ustawienia Markova, niezależne od muzycznej bazy."""

    def __init__(self, path: str = MARKOV_DATABASE_FILE) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS markov_messages (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_markov_messages_channel
                    ON markov_messages(channel_id, message_id);
                CREATE TABLE IF NOT EXISTS markov_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self.connection.commit()

    def add_message(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_id: int,
        content: str,
        created_at: float | None = None,
    ) -> bool:
        inserted = self.add_messages(
            [
                {
                    "message_id": message_id,
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "author_id": author_id,
                    "content": content,
                    "created_at": created_at,
                }
            ]
        )
        return bool(inserted)

    def add_messages(self, messages: Iterable[dict[str, object]]) -> list[str]:
        """Zapisz wiele wiadomości w jednej transakcji i zwróć nowe treści."""

        inserted_contents: list[str] = []
        now = time.time()
        with self._lock:
            for message in messages:
                content = str(message.get("content") or "")
                cursor = self.connection.execute(
                    """INSERT OR IGNORE INTO markov_messages(
                        message_id, guild_id, channel_id, author_id,
                        content, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        int(message["message_id"]),
                        int(message["guild_id"]),
                        int(message["channel_id"]),
                        int(message["author_id"]),
                        content,
                        float(message.get("created_at") or now),
                    ),
                )
                if cursor.rowcount:
                    inserted_contents.append(content)
            self.connection.commit()
        return inserted_contents

    def corpus(self) -> list[str]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT content FROM markov_messages ORDER BY message_id"
            ).fetchall()
        return [str(row["content"]) for row in rows]

    def message_count(self, channel_id: int) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS total FROM markov_messages WHERE channel_id = ?",
                (int(channel_id),),
            ).fetchone()
        return int(row["total"] or 0)

    def history_cursor(self, channel_id: int) -> int | None:
        value = self.setting(f"history_cursor:{int(channel_id)}", "")
        return _integer(value, 0) or None

    def set_history_cursor(self, channel_id: int, message_id: int) -> None:
        self.set_setting(f"history_cursor:{int(channel_id)}", int(message_id))

    def setting(self, key: str, default: str) -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM markov_settings WHERE key = ?",
                (str(key),),
            ).fetchone()
        return str(row["value"]) if row else str(default)

    def set_setting(self, key: str, value: object) -> None:
        with self._lock:
            self.connection.execute(
                """INSERT INTO markov_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(key), str(value)),
            )
            self.connection.commit()

    def advance_counter(self, every: int) -> bool:
        current = _integer(self.setting("ordinary_message_counter", "0"), 0) + 1
        due = current >= max(1, int(every))
        self.set_setting("ordinary_message_counter", 0 if due else current)
        return due

    def stats(self) -> dict[str, int]:
        with self._lock:
            row = self.connection.execute(
                """SELECT COUNT(*) AS messages,
                    COUNT(DISTINCT author_id) AS users
                FROM markov_messages"""
            ).fetchone()
        return {
            "messages": int(row["messages"] or 0),
            "users": int(row["users"] or 0),
        }


def sanitize_markov_message(content: object, bot_user_id: int | None) -> str:
    """Usuń niebezpieczne elementy, zachowując pingi innych użytkowników."""

    text = str(content or "")
    if bot_user_id:
        text = re.sub(rf"<@!?{int(bot_user_id)}>", " ", text)
    text = _URL_RE.sub(" ", text)
    text = _MASS_MENTION_RE.sub(" ", text)
    text = _ROLE_MENTION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()[:1500]


class MarkovService:
    """Łączy trwały model z historią i zdarzeniami Discorda."""

    def __init__(self, client: discord.Client, store: MarkovStore | None = None) -> None:
        self.client = client
        self.store = store or MarkovStore()
        self.model = KotoneMarkovBot()
        self.model.read_many(self.store.corpus())
        self._bootstrap_lock = asyncio.Lock()
        self._recent_response_keys: deque[str] = deque(maxlen=50)
        self.bootstrap_running = False
        self.bootstrap_added = 0

    @property
    def enabled(self) -> bool:
        default = "1" if MARKOV_DEFAULT_ENABLED else "0"
        return self.store.setting("enabled", default) == "1"

    def set_enabled(self, enabled: bool) -> None:
        self.store.set_setting("enabled", "1" if enabled else "0")

    async def bootstrap_history(self) -> int:
        """Przy pierwszym starcie pobierz historię, potem tylko nowe wpisy."""

        import discord

        async with self._bootstrap_lock:
            self.bootstrap_running = True
            added = 0
            try:
                channel = self.client.get_channel(MARKOV_CHANNEL_ID)
                if channel is None:
                    channel = await self.client.fetch_channel(MARKOV_CHANNEL_ID)
                if not hasattr(channel, "history"):
                    raise RuntimeError("Skonfigurowany kanał nie udostępnia historii.")
                channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
                if int(channel_guild_id or 0) != GUILD_ID:
                    raise RuntimeError(
                        "Kanał Markova nie należy do serwera skonfigurowanego w Kotone."
                    )

                cursor = self.store.history_cursor(MARKOV_CHANNEL_ID)
                stored_count = self.store.message_count(MARKOV_CHANNEL_ID)
                if cursor and stored_count >= MARKOV_HISTORY_LIMIT:
                    history = [
                        message
                        async for message in channel.history(
                            limit=None,
                            oldest_first=True,
                            after=discord.Object(id=cursor),
                        )
                    ]
                else:
                    # Pierwsze uruchomienie bierze ograniczoną próbkę. Ten sam
                    # wariant uzupełnia starszą instalację po zwiększeniu limitu
                    # (np. z 200 do 1000); INSERT OR IGNORE nie zapisze ponownie
                    # wcześniejszych Discord message ID.
                    newest = [
                        message
                        async for message in channel.history(
                            limit=MARKOV_HISTORY_LIMIT,
                            oldest_first=False,
                        )
                    ]
                    history = list(reversed(newest))

                prepared_messages: list[dict[str, object]] = []
                for message in history:
                    if getattr(message.author, "bot", False):
                        continue
                    content = sanitize_markov_message(
                        message.content,
                        getattr(self.client.user, "id", None),
                    )
                    if not content:
                        continue
                    prepared_messages.append(
                        {
                            "message_id": message.id,
                            "guild_id": getattr(message.guild, "id", GUILD_ID),
                            "channel_id": message.channel.id,
                            "author_id": message.author.id,
                            "content": content,
                            "created_at": message.created_at.timestamp(),
                        }
                    )
                inserted_contents = await asyncio.to_thread(
                    self.store.add_messages,
                    prepared_messages,
                )
                await asyncio.to_thread(self.model.read_many, inserted_contents)
                added = len(inserted_contents)
                if history:
                    # Kursor historii jest niezależny od wiadomości zapisanych
                    # na żywo, aby wiadomość wysłana tuż po deployu nie mogła
                    # przeskoczyć luki pomiędzy dwoma uruchomieniami.
                    self.store.set_history_cursor(
                        MARKOV_CHANNEL_ID,
                        max(int(message.id) for message in history),
                    )
                self.bootstrap_added = added
                print(
                    f"[MARKOV] Historia kanału {MARKOV_CHANNEL_ID}: "
                    f"dodano {added} nowych wiadomości."
                )
                return added
            finally:
                self.bootstrap_running = False

    async def handle_message(self, message: discord.Message) -> None:
        """Zapisz wiadomość człowieka i ewentualnie wygeneruj odpowiedź."""

        import discord

        if getattr(message.author, "bot", False) or message.guild is None:
            return
        if int(message.guild.id) != GUILD_ID:
            return
        bot_user = self.client.user
        if bot_user is None:
            return

        in_learning_channel = int(message.channel.id) == MARKOV_CHANNEL_ID
        # Moduł ma być całkowicie niewidoczny poza jednym wyznaczonym
        # kanałem: nie uczy się tam i nie odpowiada nawet po oznaczeniu.
        if not in_learning_channel:
            return
        if not self.enabled:
            return

        mentioned = bot_user in getattr(message, "mentions", []) or bool(
            re.search(rf"<@!?{int(bot_user.id)}>", str(message.content or ""))
        )

        content = sanitize_markov_message(message.content, bot_user.id)
        if content:
            inserted = self.store.add_message(
                message_id=message.id,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                author_id=message.author.id,
                content=content,
                created_at=message.created_at.timestamp(),
            )
            if inserted:
                self.model.read_text(content)

        spontaneous = not mentioned and self.store.advance_counter(
            MARKOV_SPONTANEOUS_EVERY
        )
        if not mentioned and not spontaneous:
            return

        seeds = [
            word
            for word in content.split()
            if not word.startswith("<@") and len(word) > 2
        ]
        response = ""
        current_key = _comparison_key(content)
        # Najpierw próbujemy odpowiedzi związanej z bieżącą wiadomością. Jeśli
        # mały korpus potrafi zbudować wyłącznie jej kopię, przechodzimy do
        # całego słownika zamiast od razu pokazywać komunikat awaryjny.
        for attempt_seeds, attempts in ((seeds, 10), (None, 30)):
            for _ in range(attempts):
                candidate = self.model.generate_text(
                    MARKOV_MAX_WORDS,
                    seedword=attempt_seeds,
                )
                candidate_key = _comparison_key(candidate)
                if not candidate_key or candidate_key == current_key:
                    continue
                if candidate_key in self._recent_response_keys:
                    continue
                response = candidate
                self._recent_response_keys.append(candidate_key)
                break
            if response:
                break

        # Gdy niewielki korpus zawiera bardzo mało różnych wypowiedzi, wolno
        # powtórzyć wcześniejszą odpowiedź Kotone, ale nadal nigdy bieżącą
        # wiadomość użytkownika.
        if not response:
            for _ in range(30):
                candidate = self.model.generate_text(MARKOV_MAX_WORDS)
                if _comparison_key(candidate) not in {"", current_key}:
                    response = candidate
                    break
        if not response:
            response = "Jeszcze zbieram słowa."
        response = sanitize_markov_message(response, bot_user.id)
        await message.reply(
            response[:2000],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
                replied_user=False,
            ),
        )

    def stats(self) -> dict[str, int | bool]:
        return {
            **self.store.stats(),
            **self.model.stats(),
            "enabled": self.enabled,
            "channel_id": MARKOV_CHANNEL_ID,
            "bootstrap_running": self.bootstrap_running,
        }

    def close(self) -> None:
        self.store.connection.close()
