"""Small shared helpers for local AOTY HTML conversion."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


AOTY_BASE_URL = "https://www.albumoftheyear.org"
HTML_SUFFIXES = {".html", ".htm"}


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def split_values(value: object) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    return [clean_text(part) for part in re.split(r"[,;|]", text) if clean_text(part)]


def soup_from_path(path: Path) -> BeautifulSoup:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1250", "latin-1"):
        try:
            return BeautifulSoup(data.decode(encoding), "html.parser")
        except UnicodeDecodeError:
            continue
    return BeautifulSoup(data.decode("utf-8", errors="replace"), "html.parser")


def absolute_url(value: str | None) -> str:
    value = clean_text(value)
    return urljoin(AOTY_BASE_URL, value) if value else ""


def aoty_album_id(value: str | None) -> str:
    match = re.search(r"/album/(\d+)(?:[-/?#]|$)", str(value or ""), re.I)
    return match.group(1) if match else ""


def aoty_profile_name(value: str | None) -> str:
    match = re.search(r"/user/([^/?#]+)/", str(value or ""), re.I)
    return clean_text(match.group(1)) if match else ""


def canonical_url(soup: BeautifulSoup) -> str:
    tag = soup.select_one('link[rel="canonical"]')
    return absolute_url(tag.get("href")) if tag else ""


def image_url(soup: BeautifulSoup) -> str:
    for selector, attribute in (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
    ):
        tag = soup.select_one(selector)
        if tag and clean_text(tag.get(attribute)):
            return absolute_url(tag.get(attribute))
    return ""


def tag_image_url(tag) -> str:
    """Read a CDN image from an ``img`` inside a browser-saved page."""

    if not tag:
        return ""
    for attribute in ("data-srcset", "srcset"):
        raw = clean_text(tag.get(attribute))
        if raw:
            candidate = clean_text(raw.split(",", 1)[0].split(" ", 1)[0])
            if candidate.startswith(("http://", "https://", "//")):
                return absolute_url(candidate)
    for attribute in ("data-src", "src"):
        candidate = clean_text(tag.get(attribute))
        if candidate.startswith(("http://", "https://", "//")):
            return absolute_url(candidate)
    return ""


def first_href(soup: BeautifulSoup, pattern: str) -> str:
    tag = soup.find("a", href=re.compile(pattern, re.I))
    return absolute_url(tag.get("href")) if tag else ""


def all_html_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.casefold() in HTML_SUFFIXES
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_csv(path: Path, headers: Iterable[str], rows: Iterable[dict]) -> None:
    """Atomically write UTF-8 CSV so source HTML can be deleted safely."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(headers)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: clean_text(row.get(key, "")) for key in fieldnames})
    temporary.replace(path)


def csv_bytes(headers: Iterable[str], rows: Iterable[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(headers), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def date_yyyy_mm_dd(value: object) -> str:
    """Convert common visible AOTY dates without guessing missing values."""

    text = clean_text(value)
    if not text:
        return ""
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if match:
        return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month_names = "|".join(months)
    match = re.search(
        rf"\b({month_names})\s+(\d{{1,2}}),?\s+(\d{{4}})\b",
        text,
        re.I,
    )
    if match:
        return f"{match.group(3)}-{months[match.group(1).casefold()]:02d}-{int(match.group(2)):02d}"
    match = re.search(
        rf"\b(\d{{1,2}})\s+({month_names})\s+(\d{{4}})\b",
        text,
        re.I,
    )
    if match:
        return f"{match.group(3)}-{months[match.group(2).casefold()]:02d}-{int(match.group(1)):02d}"
    return ""


def page_url_from_file(soup: BeautifulSoup, path: Path) -> str:
    """Use the saved canonical address; never infer a network address from disk."""

    canonical = canonical_url(soup)
    if canonical:
        return canonical
    for tag in soup.select("a[href]"):
        href = absolute_url(tag.get("href"))
        if "albumoftheyear.org" in urlparse(href).netloc:
            return href
    return ""
