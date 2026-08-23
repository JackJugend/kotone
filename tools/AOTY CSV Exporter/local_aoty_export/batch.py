"""One-command folder workflow for the local AOTY exporter."""

from __future__ import annotations

import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from shutil import rmtree

from .album import ALBUM_HEADERS, TRACK_HEADERS, parse_album_page
from .artist import ARTIST_HEADERS, parse_artist_page
from .common import all_html_files, read_csv, write_csv
from .profile import PROFILE_HEADERS, parse_profile_page


PROFILE_FOLDER = "1. profile"
ALBUM_FOLDER = "2. album"
ARTIST_FOLDER = "3. artist"
OUTPUT_FOLDER = "0. GOTOWE CSV"
LEGACY_OUTPUT_ZIP_NAME = "0. GOTOWE CSV.zip"
FOLDER_NAMES = (PROFILE_FOLDER, ALBUM_FOLDER, ARTIST_FOLDER, OUTPUT_FOLDER)


def _merge_rows(path: Path, headers: tuple[str, ...], rows: list[dict], key_fields: tuple[str, ...]) -> int:
    """Merge new records with an existing generated CSV, keeping one key."""

    _old_headers, existing = read_csv(path)
    merged: dict[tuple[str, ...], dict] = {}
    anonymous: list[dict] = []
    for row in [*existing, *rows]:
        key = tuple(str(row.get(field) or "").strip().casefold() for field in key_fields)
        if any(key):
            merged[key] = row
        else:
            anonymous.append(row)
    write_csv(path, headers, [*merged.values(), *anonymous])
    return len(merged) + len(anonymous)


def _remove_saved_page_assets(path: Path, problems: list[str]) -> None:
    """Delete only the Ctrl+S asset directory paired with one processed HTML."""

    assets = path.parent / f"{path.stem}_files"
    if not assets.is_dir():
        return
    try:
        # A path created from the HTML stem must stay in the same input folder.
        assets.resolve().relative_to(path.parent.resolve())
        rmtree(assets)
    except (OSError, ValueError) as exc:
        problems.append(f"nie usunieto folderu {assets.name}: {exc}")


def _remove_orphaned_asset_directories(root: Path, problems: list[str]) -> None:
    """Remove old Ctrl+S ``*_files`` folders after their HTML is gone."""

    for folder_name in (PROFILE_FOLDER, ALBUM_FOLDER, ARTIST_FOLDER):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for assets in folder.glob("*_files"):
            if not assets.is_dir():
                continue
            stem = assets.name[:-len("_files")]
            if (folder / f"{stem}.html").exists() or (folder / f"{stem}.htm").exists():
                continue
            try:
                assets.resolve().relative_to(folder.resolve())
                rmtree(assets)
            except (OSError, ValueError) as exc:
                problems.append(f"nie usunieto folderu {assets.name}: {exc}")


def _remove_legacy_output_zip(root: Path, problems: list[str]) -> None:
    """Remove the old root-level archive created by earlier exporter versions."""

    legacy_archive = root / LEGACY_OUTPUT_ZIP_NAME
    if not legacy_archive.is_file():
        return
    try:
        legacy_archive.unlink()
    except OSError as exc:
        problems.append(f"nie usunieto starego ZIP {legacy_archive.name}: {exc}")


def _run_profiles(root: Path, output: Path) -> tuple[int, list[str]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    processed: list[Path] = []
    problems: list[str] = []
    for path in all_html_files(root / PROFILE_FOLDER):
        try:
            page = parse_profile_page(path)
            groups[page.username.casefold()].extend(page.rows)
            processed.append(path)
        except Exception as exc:
            problems.append(f"{PROFILE_FOLDER}/{path.name}: {exc}")
    written = 0
    successfully_written: set[str] = set()
    for username, rows in groups.items():
        try:
            count = _merge_rows(
                output / f"profile-{username}-ratings.csv",
                PROFILE_HEADERS,
                rows,
                ("Album ID", "Artist", "Album", "Date Rated"),
            )
            written += count
            successfully_written.add(username)
            print(f"[PROFILE] {username}: {len(rows)} nowych rekordow, razem {count}.")
        except Exception as exc:
            problems.append(f"{OUTPUT_FOLDER}/profile-{username}-ratings.csv: {exc}")
    for path in processed:
        try:
            username = parse_profile_page(path).username.casefold()
            if username in successfully_written:
                path.unlink()
                _remove_saved_page_assets(path, problems)
        except OSError as exc:
            problems.append(f"nie usunieto {path.name}: {exc}")
    return written, problems


def _run_albums(root: Path, output: Path) -> tuple[int, list[str]]:
    metadata: list[dict] = []
    tracks: list[dict] = []
    processed: list[Path] = []
    problems: list[str] = []
    for path in all_html_files(root / ALBUM_FOLDER):
        try:
            page = parse_album_page(path)
            metadata.append(page.metadata)
            tracks.extend(page.tracks)
            processed.append(path)
        except Exception as exc:
            problems.append(f"{ALBUM_FOLDER}/{path.name}: {exc}")
    if not processed:
        return 0, problems
    try:
        metadata_count = _merge_rows(
            output / "album-metadata.csv", ALBUM_HEADERS, metadata, ("Album ID",)
        )
        track_count = _merge_rows(
            output / "album-tracklist.csv", TRACK_HEADERS, tracks,
            ("Album ID", "Disc", "Number", "Title"),
        ) if tracks else 0
    except Exception as exc:
        return 0, [*problems, f"{OUTPUT_FOLDER}/album-*.csv: {exc}"]
    for path in processed:
        try:
            path.unlink()
            _remove_saved_page_assets(path, problems)
        except OSError as exc:
            problems.append(f"nie usunieto {path.name}: {exc}")
    print(f"[ALBUM] {len(metadata)} stron, metadata razem {metadata_count}, tracki razem {track_count}.")
    return len(metadata) + len(tracks), problems


def _run_artists(root: Path, output: Path) -> tuple[int, list[str]]:
    rows: list[dict] = []
    processed: list[Path] = []
    problems: list[str] = []
    for path in all_html_files(root / ARTIST_FOLDER):
        try:
            page = parse_artist_page(path)
            rows.extend(page.rows)
            processed.append(path)
        except Exception as exc:
            problems.append(f"{ARTIST_FOLDER}/{path.name}: {exc}")
    if not processed:
        return 0, problems
    try:
        count = _merge_rows(
            output / "artist-discography.csv", ARTIST_HEADERS, rows,
            ("Artist", "Album ID", "Album"),
        )
    except Exception as exc:
        return 0, [*problems, f"{OUTPUT_FOLDER}/artist-discography.csv: {exc}"]
    for path in processed:
        try:
            path.unlink()
            _remove_saved_page_assets(path, problems)
        except OSError as exc:
            problems.append(f"nie usunieto {path.name}: {exc}")
    print(f"[ARTIST] {len(processed)} stron, razem {count} rekordow.")
    return len(rows), problems


def _write_output_zip(root: Path, output: Path) -> Path | None:
    """Create one portable archive containing every generated CSV."""

    csv_paths = sorted(path for path in output.glob("*.csv") if path.is_file())
    if not csv_paths:
        return None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    destination = output / f"{timestamp}.zip"
    temporary = output / f".{timestamp}.zip.tmp"
    # Write to a temporary file so a failed run never replaces an older,
    # complete archive. Only base names are included in the ZIP.
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in csv_paths:
            archive.write(path, arcname=path.name)
    temporary.replace(destination)
    return destination


def run_batch() -> int:
    root = Path(__file__).resolve().parent.parent
    for name in FOLDER_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    output = root / OUTPUT_FOLDER
    print("AOTY CSV Exporter - tylko lokalne pliki HTML")
    print(f"Folder: {root}")
    try:
        profile_count, profile_problems = _run_profiles(root, output)
        album_count, album_problems = _run_albums(root, output)
        artist_count, artist_problems = _run_artists(root, output)
    except ModuleNotFoundError as exc:
        if exc.name == "bs4":
            print("Brakuje BeautifulSoup. Wpisz: py -m pip install -r requirements.txt")
            return 2
        raise
    problems = [*profile_problems, *album_problems, *artist_problems]
    _remove_orphaned_asset_directories(root, problems)
    _remove_legacy_output_zip(root, problems)
    if not any((profile_count, album_count, artist_count)):
        print(
            "Nie znaleziono nowych poprawnych HTML. Wrzuc pliki do "
            f"{PROFILE_FOLDER}, {ALBUM_FOLDER} albo {ARTIST_FOLDER}."
        )
    else:
        print(f"Gotowe CSV: {output}")
    archive = _write_output_zip(root, output)
    if archive is not None:
        print(f"ZIP do /dbimport: {archive}")
    if problems:
        print("\nPliki pozostawione do poprawy:")
        for item in problems:
            print(f"- {item}")
        return 1
    print("Wszystkie poprawnie przetworzone HTML zostaly usuniete.")
    return 0


if __name__ == "__main__":
    sys.exit(run_batch())
