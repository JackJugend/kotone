"""Offline coverage for supported Last.fm CSV layouts and UTC normalisation."""

from __future__ import annotations

import unittest

from lastfm_import import parse_lastfm_scrobbles_csv


class LastFMImportParserTests(unittest.TestCase):
    def test_legacy_four_column_csv_is_parsed_newest_first(self):
        parsed = parse_lastfm_scrobbles_csv(
            b'gore\xc5\x9fhit,gnb,hold my hand,"19 Aug 2026, 20:48"\n'
            b'gore\xc5\x9fhit,gnb,lonely,"19 Aug 2026, 20:39"\n'
        )
        self.assertEqual(parsed["format"], "lastfm-legacy")
        self.assertEqual(parsed["tracks"][0]["played_at"], 1787172480)
        self.assertEqual(parsed["tracks"][0]["album"], "gnb")

    def test_mbid_export_uses_identifier_and_deduplicates_exact_event(self):
        payload = (
            "uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid\n"
            '"1786968846","17 Aug 2026, 12:14","WIV","artist-id","i love u.",'
            '"album-id","i love u.","track-id"\n'
            '"1786968846","17 Aug 2026, 12:14","WIV renamed","artist-id",'
            '"i love u.","album-id","i love u.","track-id"\n'
        ).encode("utf-8")
        parsed = parse_lastfm_scrobbles_csv(payload)
        self.assertEqual(parsed["format"], "lastfm-mbid")
        self.assertEqual(len(parsed["tracks"]), 1)
        self.assertEqual(parsed["duplicates"], 1)
        self.assertEqual(parsed["tracks"][0]["track_mbid"], "track-id")

    def test_csv_above_former_row_limit_is_accepted(self):
        # Full Last.fm exports commonly exceed 100k scrobbles.  The only
        # import boundary is the 100 MiB attachment limit, not row count.
        payload = b'gore\xc5\x9fhit,gnb,hold my hand,"19 Aug 2026, 20:48"\n' * 100_001
        parsed = parse_lastfm_scrobbles_csv(payload)
        self.assertEqual(len(parsed["tracks"]), 1)
        self.assertEqual(parsed["duplicates"], 100_000)


if __name__ == "__main__":
    unittest.main()
