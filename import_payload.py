"""Safe, offline CSV extraction for Discord attachment imports."""

from __future__ import annotations

import gzip
import io
import zipfile


class ImportPayloadError(ValueError):
    """The attachment is not one safe CSV payload."""


def extract_csv_payload(filename: object, payload: bytes, *, max_bytes: int) -> tuple[bytes, str]:
    """Return a CSV from ``.csv``, ``.csv.gz`` or a single-file ``.zip``.

    Extraction is entirely local.  Both the uploaded archive and the expanded
    CSV are bounded, and ZIP archives may contain exactly one CSV, preventing
    accidental multi-file imports and decompression-bomb behaviour.
    """

    name = str(filename or "").strip()
    lower_name = name.casefold()
    maximum = max(1, int(max_bytes))
    if not payload:
        raise ImportPayloadError("plik jest pusty")
    if len(payload) > maximum:
        raise ImportPayloadError("plik przekracza limit 100 MB")

    if lower_name.endswith(".csv"):
        return payload, "CSV"

    if lower_name.endswith((".csv.gz", ".gz")):
        try:
            expanded = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise ImportPayloadError("niepoprawny archiwum gzip") from exc
        if len(expanded) > maximum:
            raise ImportPayloadError("CSV po rozpakowaniu przekracza limit 100 MB")
        return expanded, "CSV.GZ"

    if lower_name.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                csv_entries = [
                    item
                    for item in archive.infolist()
                    if not item.is_dir() and item.filename.casefold().endswith(".csv")
                ]
                if len(csv_entries) != 1:
                    raise ImportPayloadError(
                        "ZIP musi zawierać dokładnie jeden plik .csv"
                    )
                entry = csv_entries[0]
                if entry.file_size > maximum:
                    raise ImportPayloadError(
                        "CSV po rozpakowaniu przekracza limit 100 MB"
                    )
                with archive.open(entry, "r") as csv_file:
                    expanded = csv_file.read(maximum + 1)
        except ImportPayloadError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ImportPayloadError("niepoprawne archiwum ZIP") from exc
        if len(expanded) > maximum:
            raise ImportPayloadError("CSV po rozpakowaniu przekracza limit 100 MB")
        return expanded, "ZIP → CSV"

    raise ImportPayloadError("załącz plik .csv, .csv.gz albo .zip z jednym plikiem CSV")
