import asyncio
import datetime as dt
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from markov_service import (
    MARKOV_CHANNEL_ID,
    MARKOV_HISTORY_LIMIT,
    KotoneMarkovBot,
    MarkovService,
    MarkovStore,
    sanitize_markov_message,
)
from settings import GUILD_ID


class _DiscordObject:
    def __init__(self, *, id):
        self.id = id


class _AllowedMentions:
    def __init__(self, **kwargs):
        self.options = kwargs


FAKE_DISCORD = types.SimpleNamespace(
    Object=_DiscordObject,
    AllowedMentions=_AllowedMentions,
)


class _User:
    def __init__(self, user_id, *, bot=False):
        self.id = user_id
        self.bot = bot


class _Message:
    def __init__(
        self,
        message_id,
        *,
        channel_id=MARKOV_CHANNEL_ID,
        guild_id=GUILD_ID,
        author=None,
        content="ten kanał buduje model markowa",
        mentions=None,
    ):
        self.id = message_id
        self.guild = types.SimpleNamespace(id=guild_id)
        self.channel = types.SimpleNamespace(id=channel_id)
        self.author = author or _User(message_id + 1000)
        self.content = content
        self.mentions = list(mentions or [])
        self.created_at = dt.datetime.now(dt.timezone.utc)
        self.replies = []

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))


class _HistoryChannel:
    def __init__(self, messages):
        self.id = MARKOV_CHANNEL_ID
        self.guild = types.SimpleNamespace(id=GUILD_ID)
        self.messages = list(messages)

    def history(self, *, limit, oldest_first, after=None):
        messages = self.messages
        if after is not None:
            messages = [message for message in messages if message.id > after.id]
        messages = sorted(messages, key=lambda message: message.id)
        if not oldest_first:
            messages.reverse()
        if limit is not None:
            messages = messages[:limit]

        async def iterator():
            for message in messages:
                yield message

        return iterator()


class _Client:
    def __init__(self, channel=None):
        self.user = _User(999, bot=True)
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == MARKOV_CHANNEL_ID else None

    async def fetch_channel(self, channel_id):
        if self.channel is None or channel_id != MARKOV_CHANNEL_ID:
            raise LookupError(channel_id)
        return self.channel


class MarkovModelTests(unittest.TestCase):
    def test_model_learns_and_generates_second_order_text(self):
        model = KotoneMarkovBot()
        model.read_text("ten album jest bardzo dobry")
        model.read_text("ten album jest bardzo dziwny")

        generated = model.generate_text(12, seedword="album")

        self.assertIn("album", generated)
        self.assertFalse(generated.endswith("."))
        self.assertEqual(model.stats()["transitions"], 5)

    def test_sanitizer_keeps_other_user_ping_but_removes_bot_ping(self):
        cleaned = sanitize_markov_message(
            "<@123> hej <@456> @everyone https://example.com <@&999>",
            123,
        )

        self.assertNotIn("<@123>", cleaned)
        self.assertIn("<@456>", cleaned)
        self.assertNotIn("@everyone", cleaned)
        self.assertNotIn("http", cleaned)
        self.assertNotIn("<@&999>", cleaned)


class MarkovStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.store = MarkovStore(self.path)

    def tearDown(self):
        self.store.connection.close()
        os.unlink(self.path)

    def test_messages_are_deduplicated_and_counter_fires_on_fifteenth(self):
        values = dict(
            message_id=1,
            guild_id=2,
            channel_id=3,
            author_id=4,
            content="pierwsza wiadomość",
            created_at=5,
        )
        self.assertTrue(self.store.add_message(**values))
        self.assertFalse(self.store.add_message(**values))
        self.assertEqual(self.store.stats()["messages"], 1)

        for _ in range(14):
            self.assertFalse(self.store.advance_counter(15))
        self.assertTrue(self.store.advance_counter(15))
        self.assertFalse(self.store.advance_counter(15))


class MarkovServiceTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.store = MarkovStore(self.path)

    def tearDown(self):
        self.store.connection.close()
        os.unlink(self.path)

    def test_first_history_import_is_capped_then_fetches_only_new(self):
        messages = [
            _Message(message_id)
            for message_id in range(1, MARKOV_HISTORY_LIMIT + 101)
        ]
        channel = _HistoryChannel(messages)
        service = MarkovService(_Client(channel), self.store)

        with patch.dict(sys.modules, {"discord": FAKE_DISCORD}):
            self.assertEqual(
                asyncio.run(service.bootstrap_history()),
                MARKOV_HISTORY_LIMIT,
            )
            self.assertEqual(
                self.store.stats()["messages"],
                MARKOV_HISTORY_LIMIT,
            )
            latest_id = MARKOV_HISTORY_LIMIT + 100
            self.assertEqual(
                self.store.history_cursor(MARKOV_CHANNEL_ID),
                latest_id,
            )

            channel.messages.extend([_Message(latest_id + 1), _Message(latest_id + 2)])
            self.assertEqual(asyncio.run(service.bootstrap_history()), 2)
            self.assertEqual(
                self.store.stats()["messages"],
                MARKOV_HISTORY_LIMIT + 2,
            )

    def test_increased_limit_backfills_older_messages_without_duplicates(self):
        newest_id = MARKOV_HISTORY_LIMIT + 100
        messages = [_Message(message_id) for message_id in range(1, newest_id + 1)]
        existing_count = 200
        for message in messages[-existing_count:]:
            self.store.add_message(
                message_id=message.id,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                author_id=message.author.id,
                content=message.content,
                created_at=message.created_at.timestamp(),
            )
        self.store.set_history_cursor(MARKOV_CHANNEL_ID, newest_id)
        service = MarkovService(_Client(_HistoryChannel(messages)), self.store)

        with patch.dict(sys.modules, {"discord": FAKE_DISCORD}):
            added = asyncio.run(service.bootstrap_history())

        self.assertEqual(added, MARKOV_HISTORY_LIMIT - existing_count)
        self.assertEqual(
            self.store.message_count(MARKOV_CHANNEL_ID),
            MARKOV_HISTORY_LIMIT,
        )

    def test_history_import_ignores_bot_messages(self):
        messages = [
            _Message(1),
            _Message(2, author=_User(777, bot=True)),
            _Message(3),
        ]
        service = MarkovService(_Client(_HistoryChannel(messages)), self.store)

        with patch.dict(sys.modules, {"discord": FAKE_DISCORD}):
            added = asyncio.run(service.bootstrap_history())

        self.assertEqual(added, 2)
        self.assertEqual(self.store.message_count(MARKOV_CHANNEL_ID), 2)

    def test_service_ignores_bots_and_every_channel_except_configured_one(self):
        client = _Client()
        service = MarkovService(client, self.store)
        outside = _Message(
            1,
            channel_id=MARKOV_CHANNEL_ID + 1,
            content=f"<@{client.user.id}> odpowiedz",
            mentions=[client.user],
        )
        bot_message = _Message(2, author=_User(123, bot=True))
        outside_guild = _Message(
            3,
            guild_id=GUILD_ID + 1,
            content=f"<@{client.user.id}> odpowiedz",
            mentions=[client.user],
        )

        with patch.dict(sys.modules, {"discord": FAKE_DISCORD}):
            asyncio.run(service.handle_message(outside))
            asyncio.run(service.handle_message(bot_message))
            asyncio.run(service.handle_message(outside_guild))

        self.assertEqual(outside.replies, [])
        self.assertEqual(outside_guild.replies, [])
        self.assertEqual(self.store.stats()["messages"], 0)

    def test_service_replies_on_mention_and_every_fifteenth_plain_message(self):
        client = _Client()
        service = MarkovService(client, self.store)
        messages = [_Message(message_id) for message_id in range(1, 16)]

        with patch.dict(sys.modules, {"discord": FAKE_DISCORD}):
            for message in messages:
                asyncio.run(service.handle_message(message))
            mentioned = _Message(
                16,
                content=f"<@{client.user.id}> co sądzisz",
                mentions=[client.user],
            )
            asyncio.run(service.handle_message(mentioned))

        self.assertTrue(all(not message.replies for message in messages[:14]))
        self.assertEqual(len(messages[14].replies), 1)
        self.assertEqual(len(mentioned.replies), 1)
        self.assertNotEqual(
            mentioned.replies[0][0].casefold().rstrip(".!?"),
            mentioned.content.casefold().rstrip(".!?"),
        )


if __name__ == "__main__":
    unittest.main()
