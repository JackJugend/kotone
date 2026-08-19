import discord

from lastfm_globals import LASTFM_ARCHIVE
from settings import (
    GUILD_ID,
    KOTONE_USERS,
    KOTONE_USERS_BY_DISCORD_ID,
    USERS,
    is_operator_discord_id,
    resolve_aoty_username,
)
from shared import configured_username_autocomplete


def setup_check_command(tree: discord.app_commands.CommandTree, monitor):
    async def config_user_autocomplete(interaction, current):
        """Wspólne autocomplete, z pełnym limitem opcji tej komendy."""

        if getattr(interaction.namespace, "source", "aoty") == "lastfm":
            needle = str(current or "").casefold()
            return [
                discord.app_commands.Choice(
                    name=f"{key} · Last.fm: {profile['lastfm_username']}"[:100],
                    value=str(key),
                )
                for key, profile in KOTONE_USERS.items()
                if profile.get("lastfm_username")
                and (
                    not needle
                    or needle in str(key).casefold()
                    or needle in str(profile["lastfm_username"]).casefold()
                )
            ][:25]
        return await configured_username_autocomplete(interaction, current, limit=25)

    @tree.command(
        name="check",
        description="Operator: ręcznie sprawdza AOTY albo najnowsze dane Last.fm",
    )
    @discord.app_commands.describe(
        source="Źródło sprawdzenia",
        username="Użytkownik Kotone (opcjonalny dla własnego konta)",
    )
    @discord.app_commands.autocomplete(username=config_user_autocomplete)
    @discord.app_commands.choices(
        source=[
            discord.app_commands.Choice(name="AOTY — monitor ocen", value="aoty"),
            discord.app_commands.Choice(name="Last.fm — najnowsze scrobble", value="lastfm"),
        ]
    )
    async def check_command(
        interaction: discord.Interaction,
        source: str = "aoty",
        username: str | None = None,
    ):
        # Komendy i tak są synchronizowane jako guild commands, ale zostawiamy
        # dodatkowe zabezpieczenie zgodne z config.json.
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        if not is_operator_discord_id(getattr(interaction.user, "id", None)):
            await interaction.response.send_message(
                "Nie masz uprawnień do `/check`.",
                ephemeral=True,
            )
            return

        if source not in {"aoty", "lastfm"}:
            await interaction.response.send_message("Nieznane źródło.", ephemeral=True)
            return

        if source == "lastfm":
            supplied = str(username or "").strip().casefold()
            if supplied:
                profile_key = supplied
            else:
                profile_key = str(
                    (KOTONE_USERS_BY_DISCORD_ID.get(interaction.user.id) or {}).get("name")
                    or ""
                ).casefold()
            profile = KOTONE_USERS.get(profile_key)
            if not profile or not profile.get("lastfm_username"):
                await interaction.response.send_message(
                    "Wybierz użytkownika Kotone z ustawionym kontem Last.fm.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            result = await LASTFM_ARCHIVE.import_newest_now(
                profile_key,
                manual_override=True,
            )
            if result.get("error"):
                await interaction.followup.send(
                    f"❌ Last.fm dla **{profile_key}**: {result['error']}"
                )
                return
            summary = result["profile"]
            await interaction.followup.send(
                f"✅ **Last.fm → {profile.get('lastfm_username')}**\n"
                f"• zapisano stronę: **{result['page']}/{result['total_pages']}**\n"
                f"• nowych scrobbli: **{result['inserted']}**\n"
                f"• łącznie: **{summary.get('total_scrobbles') or '—'}** scrobbli"
            )
            return

        username = resolve_aoty_username(interaction.user.id, username)
        if not username:
            await interaction.response.send_message(
                "Wpisz użytkownika z configu albo uruchom `/check` ze swojego "
                "konta Kotone.",
                ephemeral=True,
            )
            return

        # Autocomplete zwraca dokładne nazwy z configu, ale Discord nadal
        # pozwala ręcznie wpisać wartość. Akceptujemy więc różną wielkość
        # liter, a do monitora przekazujemy kanoniczną nazwę z config.json.
        canonical_username = next(
            (
                configured
                for configured in USERS
                if configured.casefold() == username.casefold()
            ),
            None,
        )

        if canonical_username is None:
            await interaction.response.send_message(
                "Wybierz użytkownika znajdującego się w `users` w config.json.",
                ephemeral=True,
            )
            return

        username = canonical_username

        await interaction.response.defer()
        result = await monitor.check_user(username, manual=True)

        if result.get("busy"):
            await interaction.followup.send(
                f"⏳ **{username}** jest już sprawdzany."
            )
            return

        if result.get("db_only"):
            await interaction.followup.send(
                "⏸ AOTY jest chwilowo zablokowane przez `/dbonly`; "
                "nie wysłano żadnego requestu."
            )
            return

        if result.get("error"):
            await interaction.followup.send(
                f"❌ **{username}**: {result['error']}"
            )
            return

        if result.get("seeded"):
            await interaction.followup.send(
                f"✅ **{username}**: zapisano aktualny stan ({result.get('ratings', 0)} ocen)."
            )
            return

        await interaction.followup.send(
            f"✅ **{username}** sprawdzony • "
            f"nowe: **{result.get('new', 0)}** • "
            f"zmienione: **{result.get('changed', 0)}** • "
            f"pobrane: **{result.get('ratings', 0)}**"
        )
