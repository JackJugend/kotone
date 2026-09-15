"""Polish presentation time; stored Unix timestamps remain unchanged."""

from datetime import datetime
from zoneinfo import ZoneInfo


POLISH_TIMEZONE = ZoneInfo("Europe/Warsaw")


def polish_now() -> datetime:
    return datetime.now(POLISH_TIMEZONE)


def polish_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, POLISH_TIMEZONE)
