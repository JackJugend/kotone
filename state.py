"""Compatibility module.

New code should import ``DB`` from :mod:`database`.  ``state.py`` remains as a
small stable import target so older extensions do not instantly break.
"""

from database import DB

__all__ = ["DB"]
