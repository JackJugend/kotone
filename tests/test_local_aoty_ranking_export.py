"""Regression tests for manually saved AOTY ranking pages."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "tools" / "AOTY CSV Exporter"
sys.path.insert(0, str(EXPORTER))

from local_aoty_export.ranking import parse_ranking_page  # noqa: E402


def ranking_html(url: str, *, year: str, genre_id: str = "", genre: str = "") -> str:
    return f'''<!-- saved from url=(0062){url} -->
    <html><body>
      <h1 class="headline">Best Albums of {year or "All Time"} by User Score</h1>
      <span class="genreSelect" data-year="{year or 'all'}" data-decade=""
            data-sort="weighted" data-type="userHighestRated"
            data-release-type="" data-genre-id="{genre_id}">
        <li id="genre"><div class="menuDropSelectedText">{genre or 'Genre'}</div></li>
      </span>
      <div id="rank-1" class="albumListRow">
        <h2 class="albumListTitle"><span itemprop="itemListElement">
          <span itemprop="position">1</span>
          <span itemprop="item"><a itemprop="url"
            href="/album/29250-kendrick-lamar-to-pimp-a-butterfly.php">
            Kendrick Lamar - To Pimp a Butterfly</a></span>
        </span></h2>
        <div class="albumListCover mustHear both"><img
          srcset="https://cdn2.albumoftheyear.org/400x0/album/29250-cover.jpg 2x"></div>
        <div class="albumListDate">March 15, 2015</div>
        <div class="albumListGenre"><a>Conscious Hip Hop</a>
          <div class="secondary-genres"><a>Funk</a></div>
        </div>
        <div class="scoreValueContainer" title="94.7"><div class="scoreValue">95</div></div>
        <div class="scoreText">55,414 ratings</div>
        <div class="albumListLinks">
          <a data-track-action="Spotify" href="https://open.spotify.com/album/test">Spotify</a>
        </div>
      </div>
    </body></html>'''


class LocalAOTYRankingExportTests(unittest.TestCase):
    def parse(self, html: str):
        with tempfile.TemporaryDirectory(prefix="kotone-ranking-") as folder:
            path = Path(folder) / "ranking.html"
            path.write_text(html, encoding="utf-8")
            return parse_ranking_page(path)

    def test_all_time_page_sets_only_all_time_rank(self):
        page = self.parse(ranking_html(
            "https://www.albumoftheyear.org/ratings/user-highest-rated/all/",
            year="",
        ))
        row = page.rows[0]
        self.assertEqual(row["Album ID"], "29250")
        self.assertEqual(row["Artist"], "Kendrick Lamar")
        self.assertEqual(row["Album"], "To Pimp a Butterfly")
        self.assertEqual(row["AOTY User Score"], "94.7")
        self.assertEqual(row["AOTY Ratings"], "55414")
        self.assertEqual(row["All Time Ratings"], "#1")
        self.assertEqual(row["Year Ratings"], "")
        self.assertEqual(row["Must Hear"], "both")
        self.assertEqual(row["Spotify URL"], "https://open.spotify.com/album/test")
        self.assertEqual(row["Ranking Key"], "user_score|all|weighted||")

    def test_unfiltered_year_page_sets_only_year_rank(self):
        page = self.parse(ranking_html(
            "https://www.albumoftheyear.org/ratings/user-highest-rated/2026/",
            year="2026",
        ))
        row = page.rows[0]
        self.assertEqual(row["Year Ratings"], "#1")
        self.assertEqual(row["All Time Ratings"], "")

    def test_genre_filtered_page_never_overwrites_year_rank(self):
        page = self.parse(ranking_html(
            "https://www.albumoftheyear.org/ratings/user-highest-rated/2026/alternative-metal/",
            year="2026",
            genre_id="40",
            genre="Alternative Metal",
        ))
        row = page.rows[0]
        self.assertEqual(row["Ranking Genre"], "Alternative Metal")
        self.assertEqual(row["Year Ratings"], "")
        self.assertEqual(row["All Time Ratings"], "")


if __name__ == "__main__":
    unittest.main()
