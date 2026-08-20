from __future__ import annotations

import gzip
import io
import unittest
import zipfile

from import_payload import ImportPayloadError, extract_csv_payload


class ImportPayloadTests(unittest.TestCase):
    def test_plain_csv_is_preserved(self):
        payload, kind = extract_csv_payload("history.csv", b"a,b\n", max_bytes=100)
        self.assertEqual(payload, b"a,b\n")
        self.assertEqual(kind, "CSV")

    def test_gzip_is_unpacked_without_network(self):
        payload, kind = extract_csv_payload(
            "history.csv.gz", gzip.compress(b"a,b\n"), max_bytes=100
        )
        self.assertEqual(payload, b"a,b\n")
        self.assertEqual(kind, "CSV.GZ")

    def test_zip_accepts_one_csv_only(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("history.csv", "a,b\n")
        payload, kind = extract_csv_payload("history.zip", archive.getvalue(), max_bytes=1_000)
        self.assertEqual(payload, b"a,b\n")
        self.assertEqual(kind, "ZIP → CSV")

    def test_zip_rejects_multiple_csv_files(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("one.csv", "a,b\n")
            bundle.writestr("two.csv", "a,b\n")
        with self.assertRaises(ImportPayloadError):
            extract_csv_payload("history.zip", archive.getvalue(), max_bytes=1_000)


if __name__ == "__main__":
    unittest.main()
