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
    _context_copy_limit,
    _copied_word_ratio,
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
    def test_copied_word_ratio_counts_repeated_words_from_source(self):
        self.assertEqual(_copied_word_ratio("kotone mówi", "kotone mówi teraz"), 2 / 3)
        self.assertEqual(_copied_word_ratio("inne zdanie", "kotone mówi teraz"), 0)
        self.assertEqual(_copied_word_ratio("hej hej", "hej hej kotone"), 2 / 3)
        self.assertEqual(_context_copy_limit("hej"), 1)
        self.assertEqual(_context_copy_limit("hej kotone"), 1)
        self.assertEqual(_context_copy_limit("jeden dwa trzy cztery pięć"), 2)

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

    def test_random_counter_persists_a_new_target_after_each_reply(self):
        with patch("markov_service.random.randint", side_effect=[10, 30, 20]):
            for _ in range(9):
                self.assertFalse(self.store.advance_random_counter(10, 30))
            self.assertTrue(self.store.advance_random_counter(10, 30))
            self.assertEqual(self.store.setting("ordinary_message_target", ""), "30")
            for _ in range(29):
                self.assertFalse(self.store.advance_random_counter(10, 30))
            self.assertTrue(self.store.advance_random_counter(10, 30))
            self.assertEqual(self.store.setting("ordinary_message_target", ""), "20")

    def test_batch_insert_returns_only_new_message_contents(self):
        rows = [
            {
                "message_id": message_id,
                "guild_id": 2,
                "channel_id": 3,
                "author_id": 4,
                "content": f"wiadomość {message_id}",
                "created_at": 5,
            }
            for message_id in range(1, 101)
        ]

        self.assertEqual(len(self.store.add_messages(rows)), 100)
        self.assertEqual(self.store.add_messages(rows), [])
        self.assertEqual(self.store.stats()["messages"], 100)


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

    def test_history_limit_counts_human_messages_not_bot_messages(self):
        bot_messages = [
            _Message(
                MARKOV_HISTORY_LIMIT + message_id,
                author=_User(7000 + message_id, bot=True),
            )
            for message_id in range(1, 301)
        ]
        human_messages = [
            _Message(message_id)
            for message_id in range(1, MARKOV_HISTORY_LIMIT + 1)
        ]
        channel = _HistoryChannel([*human_messages, *bot_messages])
        service = MarkovService(_Client(channel), self.store)

        with patch.dict(sys.modules, {"discord": FAKE_DISCORD}):
            added = asyncio.run(service.bootstrap_history())

        self.assertEqual(added, MARKOV_HISTORY_LIMIT)
        self.assertEqual(
            self.store.message_count(MARKOV_CHANNEL_ID),
            MARKOV_HISTORY_LIMIT,
        )

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

    def test_service_replies_on_mention_and_at_random_plain_message_threshold(self):
        client = _Client()
        service = MarkovService(client, self.store)
        messages = [_Message(message_id) for message_id in range(1, 16)]

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch("markov_service.random.randint", side_effect=[15, 20]),
            patch("markov_service.random.random", return_value=1.0),
        ):
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

    def test_seeded_echo_falls_back_to_a_different_contextual_answer(self):
        client = _Client()
        service = MarkovService(client, self.store)
        current = f"<@{client.user.id}> powiedz coś o tym albumie"
        mentioned = _Message(1, content=current, mentions=[client.user])
        generated = ["powiedz coś o tym albumie"] * 10 + [
            "albumie jest dziwne zakończenie"
        ]

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch("markov_service.random.random", return_value=1.0),
            patch.object(service.model, "generate_text", side_effect=generated),
        ):
            asyncio.run(service.handle_message(mentioned))

        self.assertEqual(mentioned.replies[0][0], "albumie jest dziwne zakończenie")

    def test_normal_mention_rejects_unrelated_candidate(self):
        client = _Client()
        service = MarkovService(client, self.store)
        mentioned = _Message(
            1,
            content=f"<@{client.user.id}> alfa beta gamma delta epsilon",
            mentions=[client.user],
        )

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch("markov_service.random.random", return_value=1.0),
            patch.object(
                service.model,
                "generate_text",
                side_effect=["zupełnie obcy tekst", "alfa odpowiedź z korpusu"],
            ),
        ):
            asyncio.run(service.handle_message(mentioned))

        self.assertEqual(mentioned.replies[0][0], "alfa odpowiedź z korpusu")

    def test_one_word_mention_can_generate_a_contextual_markov_answer(self):
        client = _Client()
        service = MarkovService(client, self.store)
        service.model.read_text("ten album ma bardzo dobry klimat")
        mentioned = _Message(
            1,
            content=f"<@{client.user.id}> album",
            mentions=[client.user],
        )

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch("markov_service.random.random", return_value=1.0),
        ):
            asyncio.run(service.handle_message(mentioned))

        self.assertIn("album", mentioned.replies[0][0].casefold())
        self.assertNotEqual(mentioned.replies[0][0].casefold(), "album")

    def test_failed_contextual_mention_falls_back_to_random_corpus_text(self):
        client = _Client()
        service = MarkovService(client, self.store)
        mentioned = _Message(
            1,
            content=f"<@{client.user.id}> alfa beta gamma delta epsilon",
            mentions=[client.user],
        )
        contextual_echoes = ["alfa beta gamma delta epsilon"] * 40

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch("markov_service.random.random", return_value=1.0),
            patch.object(
                service.model,
                "generate_text",
                side_effect=[*contextual_echoes, "zupełnie losowa wypowiedź"],
            ),
        ):
            asyncio.run(service.handle_message(mentioned))

        self.assertEqual(mentioned.replies[0][0], "zupełnie losowa wypowiedź")

    def test_response_rejects_more_than_forty_percent_of_user_words(self):
        client = _Client()
        service = MarkovService(client, self.store)
        mentioned = _Message(
            1,
            content=f"<@{client.user.id}> alfa beta gamma delta epsilon",
            mentions=[client.user],
        )
        generated = [
            "alfa beta gamma zupełnie inaczej",
            "alfa beta odpowiedź z korpusu",
        ]

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch("markov_service.random.random", return_value=1.0),
            patch.object(service.model, "generate_text", side_effect=generated),
        ):
            asyncio.run(service.handle_message(mentioned))

        self.assertEqual(mentioned.replies[0][0], "alfa beta odpowiedź z korpusu")

    def test_mention_sometimes_uses_unseeded_response_with_no_current_words(self):
        client = _Client()
        service = MarkovService(client, self.store)
        message = _Message(
            1,
            content=f"<@{client.user.id}> alfa beta gamma",
            mentions=[client.user],
        )

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch("markov_service.random.random", return_value=0.0),
            patch("markov_service.random.uniform", return_value=0.20),
            patch.object(
                service.model,
                "generate_text",
                side_effect=["alfa obca odpowiedź", "zupełnie inny tekst"],
            ) as generate,
        ):
            asyncio.run(service.handle_message(message))

        self.assertEqual(message.replies[0][0], "zupełnie inny tekst")
        self.assertTrue(all(call.kwargs["seedword"] is None for call in generate.call_args_list))

    def test_bare_mention_uses_corpus_instead_of_collection_message(self):
        client = _Client()
        service = MarkovService(client, self.store)
        message = _Message(
            1,
            content=f"<@{client.user.id}>",
            mentions=[client.user],
        )

        with (
            patch.dict(sys.modules, {"discord": FAKE_DISCORD}),
            patch.object(
                service.model,
                "generate_text",
                return_value="wiadomość z całego korpusu",
            ),
        ):
            asyncio.run(service.handle_message(message))

        self.assertEqual(message.replies[0][0], "wiadomość z całego korpusu")


if __name__ == "__main__":
    unittest.main()
