"""Regression tests for `/album` Rich Presence parsing."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import discord


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-presence-test-"))
sys.path.insert(0, str(ROOT))

from commands.album import _music_from_presence  # noqa: E402


class _ListeningActivity:
    """Minimal Discord custom RPC matching the Music card in the report."""

    type = discord.ActivityType.listening
    name = "Music"
    details = "bloodthirsty butchers - 11月/november"
    state = "KOCORONO"
    assets = {"large_text": "KOCORONO"}


class _Member:
    activities = (_ListeningActivity(),)


class AlbumPresenceTests(unittest.TestCase):
    def test_listening_rpc_uses_state_as_album_and_details_artist(self):
        self.assertEqual(
            _music_from_presence(_Member()),
            ("bloodthirsty butchers", "KOCORONO", "Music"),
        )


if __name__ == "__main__":
    unittest.main()
