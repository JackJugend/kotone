"""Repository-level checks that keep runtime data and credentials out of Git."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_tracked_config_does_not_contain_discord_token(self):
        config_path = ROOT / "config.json"
        if not config_path.exists():
            self.skipTest("config.json is not part of this checkout")

        config = json.loads(config_path.read_text(encoding="utf-8"))
        token = config.get("discord_token")
        self.assertFalse(
            isinstance(token, str) and token.strip(),
            "Keep DISCORD_TOKEN in Railway/local environment variables, not config.json",
        )

    def test_private_runtime_files_are_not_tracked(self):
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("Git metadata is unavailable in this environment")

        tracked = {
            Path(raw.decode("utf-8")).as_posix().casefold()
            for raw in completed.stdout.split(b"\0")
            if raw
        }
        forbidden_names = {
            ".env",
            "data.json",
            "data_old.json",
            "data_migrated.json.bak",
            "railway ssh",
            "railway ssh.pub",
        }
        forbidden_suffixes = {
            ".db",
            ".key",
            ".p12",
            ".pem",
            ".sqlite",
            ".sqlite3",
        }
        forbidden_fragments = {
            ".corrupt-",
            ".empty-",
            ".recovery-required",
            ".sqlite3.tmp",
            ".tmp.sqlite3",
        }

        unsafe = sorted(
            path
            for path in tracked
            if Path(path).name in forbidden_names
            or Path(path).suffix in forbidden_suffixes
            or any(fragment in path for fragment in forbidden_fragments)
        )
        self.assertEqual(
            unsafe,
            [],
            "Private runtime/credential files must stay outside Git: "
            + ", ".join(unsafe),
        )


if __name__ == "__main__":
    unittest.main()
