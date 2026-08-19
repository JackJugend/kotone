"""Private operator switch for globally pausing background AOTY checks."""

from __future__ import annotations

import discord

from http_client import HTTP
import lastfm
from services import DATA
from settings import GUILD_ID, is_operator_discord_id
from source_switches import SOURCES


def _is_operator(interaction: discord.Interaction) -> bool:
    """Only configured operators may change the global network policy."""

    return is_operator_discord_id(getattr(interaction.user, "id", None))


def _remaining(seconds: float | int | None) -> str:
    """Format a harmless diagnostic duration for the private status panel."""

    total = max(0, round(float(seconds or 0)))
    if total <= 0:
        return "brak"
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s"


def setup_dbonly_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="dbonly",
        description="Przełącznik monitorowania AOTY w tle.",
    )
    @discord.app_commands.describe(
        mode="Stan wybranego źródła",
        source="Źródło danych działające wyłącznie w tle",
    )
    @discord.app_commands.choices(
        mode=[
            discord.app_commands.Choice(
                name="Włącz — zablokuj sprawdzanie AOTY",
                value="on",
            ),
            discord.app_commands.Choice(
                name="Wyłącz — monitor znów sprawdza AOTY",
                value="off",
            ),
            discord.app_commands.Choice(name="Pokaż status", value="status"),
        ],
        source=[
            discord.app_commands.Choice(name="AOTY scraper", value="aoty"),
            discord.app_commands.Choice(name="MusicBrainz API", value="musicbrainz"),
            discord.app_commands.Choice(name="Last.fm API", value="lastfm"),
            discord.app_commands.Choice(name="Wszystkie źródła", value="all"),
        ],
    )
    async def dbonly_command(
        interaction: discord.Interaction,
        mode: str,
        source: str = "aoty",
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        if not _is_operator(interaction):
            await interaction.response.send_message(
                "Nie masz uprawnień do `/dbonly`.",
                ephemeral=True,
            )
            return

        if mode == "status":
            switches = SOURCES.status()
            aoty_status = HTTP.status()
            musicbrainz_status = DATA.musicbrainz_status()
            lastfm_status = lastfm.LASTFM.status()
            if HTTP.db_only_enabled():
                aoty_runtime = "ręcznie zablokowany"
            elif aoty_status.get("challenge_open"):
                aoty_runtime = (
                    "challenge/cooldown: "
                    f"{_remaining(aoty_status.get('challenge_seconds'))}"
                )
            elif aoty_status.get("circuit_open"):
                aoty_runtime = (
                    "awaryjny cooldown: "
                    f"{_remaining(aoty_status.get('circuit_seconds'))}"
                )
            else:
                aoty_runtime = "gotowy"

            if not switches["musicbrainz"]:
                musicbrainz_runtime = "ręcznie zablokowany"
            elif musicbrainz_status.get("blocked"):
                musicbrainz_runtime = (
                    "cooldown: "
                    f"{_remaining(musicbrainz_status.get('blocked_seconds'))}"
                )
            else:
                musicbrainz_runtime = "gotowy"

            if not lastfm_status.get("configured"):
                lastfm_runtime = "brak LASTFM_API_KEY"
            elif not switches["lastfm"]:
                lastfm_runtime = "ręcznie zablokowany"
            elif lastfm_status.get("blocked"):
                lastfm_runtime = (
                    "cooldown: "
                    f"{_remaining(lastfm_status.get('blocked_seconds'))}"
                )
            else:
                lastfm_runtime = "gotowy"
            message = "\n".join(
                (
                    "**Źródła Kotone**",
                    f"• AOTY scraper: {'⏸ zablokowany' if HTTP.db_only_enabled() else '▶ włączony'} — {aoty_runtime}",
                    f"• MusicBrainz API: {'▶ włączone' if switches['musicbrainz'] else '⏸ zablokowane'} — {musicbrainz_runtime}",
                    f"• Last.fm API: {'▶ włączone' if switches['lastfm'] else '⏸ zablokowane'} — {lastfm_runtime}",
                    "Komendy Discord czytają SQLite niezależnie od tych przełączników.",
                )
            )
        else:
            if source not in {"aoty", "musicbrainz", "lastfm", "all"}:
                await interaction.response.send_message(
                    "Nieznane źródło dla `/dbonly`.",
                    ephemeral=True,
                )
                return
            enabled = mode == "off"
            actor = str(interaction.user.id)
            if source in {"aoty", "all"}:
                HTTP.set_db_only(not enabled, actor=actor)
            if source in {"musicbrainz", "all"}:
                SOURCES.set_enabled("musicbrainz", enabled, actor=actor)
            if source in {"lastfm", "all"}:
                SOURCES.set_enabled("lastfm", enabled, actor=actor)

            source_label = {
                "aoty": "AOTY scraper",
                "musicbrainz": "MusicBrainz API",
                "lastfm": "Last.fm API",
                "all": "wszystkie źródła",
            }[source]
            state = "odblokowane" if enabled else "zablokowane"
            message = (
                f"{'▶' if enabled else '⏸'} **{source_label.capitalize()} są {state}.** "
                "Komendy nadal korzystają wyłącznie z SQLite; zmiana dotyczy "
                "tylko monitora i workera w tle."
            )

        await interaction.response.send_message(message, ephemeral=True)
