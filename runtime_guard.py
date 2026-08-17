"""Fail-fast checks for persistent production storage.

This module intentionally has no Kotone imports.  ``bot.py`` loads it before
``settings``/``database`` so a Railway deployment without an attached Volume
cannot accidentally create the production database in the disposable image.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping


_RAILWAY_MARKERS = (
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PUBLIC_DOMAIN",
    "RAILWAY_PRIVATE_DOMAIN",
)


def _clean(value) -> str:
    return str(value or "").strip()


def _production_requested(environ: Mapping[str, str]) -> bool:
    return any(
        _clean(environ.get(name)).casefold() in {"prod", "production"}
        for name in ("KOTONE_ENV", "ENVIRONMENT", "ENV")
    )


def is_railway_runtime(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return any(_clean(environ.get(name)) for name in _RAILWAY_MARKERS)


def _path_is_within(path: str, parent: str) -> bool:
    try:
        resolved_path = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        resolved_parent = os.path.normcase(os.path.realpath(os.path.abspath(parent)))
        return os.path.commonpath((resolved_path, resolved_parent)) == resolved_parent
    except (OSError, ValueError):
        return False


def validate_persistent_runtime(
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Reject production startup when SQLite would live on ephemeral disk.

    Railway must expose ``RAILWAY_VOLUME_MOUNT_PATH``.  If ``DATA_DIR`` is
    also configured there, it must point at the mount (or a child directory),
    because ``settings.py`` intentionally gives that explicit value priority.
    Non-Railway production can use an explicit ``DATA_DIR``.  Local development
    and tests remain unchanged.
    """

    environ = os.environ if environ is None else environ
    railway = is_railway_runtime(environ)
    volume_dir = _clean(environ.get("RAILWAY_VOLUME_MOUNT_PATH"))
    data_dir = _clean(environ.get("DATA_DIR"))

    if railway:
        if not volume_dir:
            raise RuntimeError(
                "Railway uruchomił Kotone bez RAILWAY_VOLUME_MOUNT_PATH. "
                "Podłącz persistent Volume przed startem bota."
            )

        if data_dir and not _path_is_within(data_dir, volume_dir):
            raise RuntimeError(
                "DATA_DIR na Railway musi wskazywać katalog na podłączonym "
                "persistent Volume."
            )

        return

    if _production_requested(environ) and not data_dir and not volume_dir:
        raise RuntimeError(
            "Tryb production wymaga trwałego DATA_DIR lub "
            "RAILWAY_VOLUME_MOUNT_PATH."
        )
