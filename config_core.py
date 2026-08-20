"""Ładowanie plików konfiguracyjnych i ścieżek danych Kotone.

Ten moduł nie zna użytkowników, Discorda ani interwałów synchronizacji.
Odpowiada wyłącznie za jednorazowe wczytanie ``config.json`` i wyliczenie
ścieżek, dzięki czemu pozostałe moduły konfiguracji nie tworzą cykli importów.
"""

from __future__ import annotations

import json
import os


# ---------------------------------------------------------------------------
# Pliki projektu
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BASE_URL = "https://www.albumoftheyear.org"

DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data.json")
DEFAULT_DATABASE_FILE = os.path.join(BASE_DIR, "kotone.sqlite3")


# ---------------------------------------------------------------------------
# Katalog trwałych danych
# ---------------------------------------------------------------------------

RAILWAY_VOLUME_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DATA_DIR = os.getenv("DATA_DIR") or RAILWAY_VOLUME_DIR

if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)

    DATA_FILE = os.path.join(DATA_DIR, "data.json")
    DATABASE_FILE = os.path.join(DATA_DIR, "kotone.sqlite3")
    LASTFM_DATABASE_FILE = os.path.join(DATA_DIR, "kotone-lastfm.sqlite3")
    MIGRATED_DATA_BACKUP_FILE = os.path.join(DATA_DIR, "data_migrated.json.bak")
else:
    DATA_FILE = DEFAULT_DATA_FILE
    DATABASE_FILE = DEFAULT_DATABASE_FILE
    LASTFM_DATABASE_FILE = os.path.join(BASE_DIR, "kotone-lastfm.sqlite3")
    MIGRATED_DATA_BACKUP_FILE = os.path.join(BASE_DIR, "data_migrated.json.bak")


# ---------------------------------------------------------------------------
# Surowa konfiguracja
# ---------------------------------------------------------------------------

with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
    CONFIG = json.load(config_file)

