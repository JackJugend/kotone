"""Persistent state used by the automatic/manual rating monitor."""

from __future__ import annotations

import json
import os
import shutil
from threading import RLock

from settings import BASE_DIR, DATA_FILE

STATE_VERSION = 4


def create_empty_state() -> dict:
    return {"version": STATE_VERSION, "users": {}}


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = RLock()
        self.data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return create_empty_state()

        try:
            with open(self.path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
        except Exception as exc:
            print(f"[DATA] Błąd odczytu data.json: {exc}")
            return create_empty_state()

        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != STATE_VERSION
            or not isinstance(loaded.get("users"), dict)
        ):
            try:
                shutil.copyfile(
                    self.path,
                    os.path.join(BASE_DIR, "data_old.json"),
                )
                print("[DATA] Stary data.json zapisano jako data_old.json.")
            except Exception:
                pass

            print("[DATA] Tworzę nową pamięć bota.")
            return create_empty_state()

        return loaded

    def save(self) -> None:
        with self._lock:
            temp_file = self.path + ".tmp"

            with open(temp_file, "w", encoding="utf-8") as file:
                json.dump(
                    self.data,
                    file,
                    indent=4,
                    ensure_ascii=False,
                )

            os.replace(temp_file, self.path)


STORE = StateStore(DATA_FILE)
