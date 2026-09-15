"""Offline checks for the chart command's options, identity and PNG response."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-chart-runtime-"))

import discord
from PIL import Image

from commands.analytics import setup_analytics_commands
from stats_engine import rating_activity
from stats_graphics import BACKGROUND, PANEL, render_chart


class ChartCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = discord.app_commands.CommandTree(self.client)
        setup_analytics_commands(self.tree)
        self.command = self.tree.get_command("chart")
        self.interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def asyncTearDown(self):
        await self.client.close()

    async def test_options_expose_requested_syntax_and_bounded_period(self):
        parameters = {parameter.name: parameter for parameter in self.command.parameters}
        self.assertEqual(set(parameters), {"type", "period", "username"})
        self.assertEqual(parameters["type"].default, "monthly")
        self.assertEqual(
            {choice.value for choice in parameters["type"].choices},
            {"daily", "weekly", "monthly", "yearly"},
        )
        self.assertEqual(parameters["period"].default, 12)
        self.assertEqual(parameters["period"].min_value, 1)
        self.assertEqual(parameters["period"].max_value, 60)
        self.assertTrue(parameters["username"].autocomplete)

    async def test_default_profile_and_explicit_username_send_saved_data_as_png(self):
        for username in (None, "ENSO"):
            with self.subTest(username=username):
                database = MagicMock()
                database.canonical_username.return_value = "enso"
                database.get_analytics_rows.return_value = [
                    {"score": "0", "sort_timestamp": datetime.now(UTC).timestamp()},
                    {"score": "NR", "sort_timestamp": datetime.now(UTC).timestamp()},
                ]
                database.get_avatar.return_value = "https://cdn.albumoftheyear.org/avatar.png"
                with (
                    patch("commands.analytics.DB", database),
                    patch("commands.analytics.resolve_aoty_username", return_value="ENSO") as resolve,
                    patch("commands.analytics.load_cover_images", return_value=[]) as images,
                ):
                    await self.command.callback(
                        self.interaction, type="monthly", period=10, username=username,
                    )
                resolve.assert_called_once_with(123, username)
                database.get_analytics_rows.assert_called_once_with("enso")
                database.get_avatar.assert_called_once_with("enso")
                images.assert_called_once_with(
                    [{"username": "enso", "cover": database.get_avatar.return_value}], limit=1,
                )
                sent_file = self.interaction.followup.send.call_args.kwargs["file"]
                self.assertEqual(sent_file.filename, "chart-enso-monthly-10.png")
                with Image.open(sent_file.fp) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.width, 1000)
                sent_file.close()
                self.interaction.response.send_message.assert_not_awaited()

    async def test_unknown_profile_and_missing_identity_do_not_read_ratings(self):
        for resolved in (None, "outsider"):
            with self.subTest(resolved=resolved):
                database = MagicMock()
                database.canonical_username.return_value = None
                with (
                    patch("commands.analytics.DB", database),
                    patch("commands.analytics.resolve_aoty_username", return_value=resolved),
                ):
                    await self.command.callback(self.interaction)
                database.get_analytics_rows.assert_not_called()
                self.assertTrue(self.interaction.response.send_message.call_args.kwargs["ephemeral"])
                self.interaction.response.defer.assert_not_awaited()
                self.interaction.followup.send.assert_not_awaited()

    async def test_invalid_type_or_period_is_rejected_before_database_read(self):
        for chart_type, period in (("hourly", 10), ("monthly", 0), ("monthly", 61)):
            with self.subTest(chart_type=chart_type, period=period):
                database = MagicMock()
                database.canonical_username.return_value = "enso"
                with (
                    patch("commands.analytics.DB", database),
                    patch("commands.analytics.resolve_aoty_username", return_value="enso"),
                ):
                    await self.command.callback(self.interaction, type=chart_type, period=period)
                database.get_analytics_rows.assert_not_called()
                self.assertTrue(self.interaction.response.send_message.call_args.kwargs["ephemeral"])
                self.interaction.response.defer.assert_not_awaited()


class ChartGraphicTests(unittest.TestCase):
    def test_empty_single_period_and_long_ranges_render_with_stats_palette(self):
        now = datetime(2026, 9, 15, 12, tzinfo=UTC)
        for chart_type, period, rows in (
            ("daily", 1, []),
            ("weekly", 60, [{"score": "80", "sort_timestamp": now.timestamp()}]),
            ("monthly", 10, [{"score": "90", "rating_date": "01.01.2026"}]),
            ("yearly", 60, [{"score": "70", "rating_date": "nieznana"}]),
        ):
            with self.subTest(chart_type=chart_type, period=period):
                data = rating_activity("użytkownik_" * 8, rows, chart_type, period, now=now)
                graphic = render_chart(data)
                with Image.open(io.BytesIO(graphic.getvalue())) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.width, 1000)
                    self.assertEqual(image.getpixel((0, 0)), BACKGROUND)
                    self.assertEqual(image.getpixel((500, 30)), PANEL)


if __name__ == "__main__":
    unittest.main()
