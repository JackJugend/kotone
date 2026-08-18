"""Ephemeral Rich Presence cache.

Activities are intentionally held only in process memory. They are never
written to SQLite and vanish on restart, matching the user's Discord privacy
state. ``on_presence_update`` is more reliable than an interaction's embedded
Member object, which may not contain activities on every gateway payload.
"""

from __future__ import annotations


class PresenceCache:
    def __init__(self) -> None:
        self._activities: dict[int, tuple[object, ...]] = {}

    def update(self, member) -> None:
        user_id = getattr(member, "id", None)
        if user_id is None:
            return
        self._activities[int(user_id)] = tuple(
            getattr(member, "activities", ()) or ()
        )

    def activities_for(self, user_id: int | None) -> tuple[object, ...]:
        if user_id is None:
            return ()
        return self._activities.get(int(user_id), ())


PRESENCE_CACHE = PresenceCache()
