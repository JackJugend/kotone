"""Wspólne zakładki wydania używane przez komendy Kotone.

Home pozostaje własnością konkretnej komendy.  Info, Tracklist i Review są
celowo umieszczone tutaj, aby każdy widok prezentował te same dane SQLite,
okładkę Must Hear i autora AOTY.
"""

from .details import build_release_details_embed
from .review import build_review_embed
from .tracklist import build_combined_tracklist_embed

__all__ = (
    "build_combined_tracklist_embed",
    "build_release_details_embed",
    "build_review_embed",
)
