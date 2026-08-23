"""Open a normal, local browser session for manual AOTY use.

This helper deliberately does not scrape pages, solve CAPTCHAs, export cookies,
or send a browser session to Railway.  It only opens an isolated local browser
profile so the user can sign in and complete any website checks themselves.
The Kotone bot remains SQLite/API/CSV based on Railway.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import webbrowser
from pathlib import Path


DEFAULT_URL = "https://www.albumoftheyear.org/"


def _browser_candidates() -> list[Path]:
    """Return conventional Chrome/Edge paths without probing browser data."""

    roots = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    relative_paths = (
        "Google/Chrome/Application/chrome.exe",
        "Microsoft/Edge/Application/msedge.exe",
    )
    return [
        Path(root) / relative
        for root in roots
        if root
        for relative in relative_paths
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Otwiera normalną, ręczną sesję AOTY dla Kotone."
    )
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    arguments = parser.parse_args()

    url = str(arguments.url or DEFAULT_URL).strip()
    if not url.startswith(("https://", "http://")):
        parser.error("URL musi zaczynać się od https:// lub http://")

    browser = next((path for path in _browser_candidates() if path.is_file()), None)
    if browser is None:
        # This still opens the user's ordinary browser; no session data is
        # copied or exposed to the bot.
        webbrowser.open(url, new=1)
        print("Otwarto AOTY w domyślnej przeglądarce.")
        return 0

    profile_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Kotone" / "AOTYManualBrowser"
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(browser), f"--user-data-dir={profile_dir}", "--new-window", url],
        close_fds=True,
    )
    print("Otwarto ręczną sesję AOTY. Zaloguj się i obsłuż challenge samodzielnie.")
    print("Sesja pozostaje tylko na tym komputerze i nie jest przekazywana do Railway.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
