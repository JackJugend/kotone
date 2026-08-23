"""Offline AOTY HTML -> CSV exporter.

The script intentionally never opens a browser nor contacts Album of the Year.
It only converts HTML files that the user has already saved locally.
"""

from __future__ import annotations

from pathlib import Path


def main() -> int:
    # Make the drag-and-drop folders visible even on a new Windows setup that
    # still needs the one-off BeautifulSoup installation.
    root = Path(__file__).resolve().parent
    for name in (
        "1. profile", "2. album", "3. artist", "4. rankings", "0. GOTOWE CSV",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    try:
        from local_aoty_export.batch import run_batch
    except ModuleNotFoundError as exc:
        if exc.name == "bs4":
            print("Brakuje BeautifulSoup. Wpisz: py -m pip install -r requirements.txt")
            return 2
        raise
    return run_batch()


if __name__ == "__main__":
    raise SystemExit(main())
