import discord

from settings import GUILD_ID, USERS


def setup_check_command(tree: discord.app_commands.CommandTree, monitor):
    async def config_user_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ):
        current = str(current or "").casefold().strip()
        matches = [
            username
            for username in USERS
            if not current or current in username.casefold()
        ]
        return [
            discord.app_commands.Choice(name=username[:100], value=username[:100])
            for username in matches[:25]
        ]

    @tree.command(
        name="check",
        description="Manualnie sprawdza aktualizacje monitorowanego usera AOTY",
    )
    @discord.app_commands.describe(
        username="User z listy users w config.json",
    )
    @discord.app_commands.autocomplete(username=config_user_autocomplete)
    async def check_command(
        interaction: discord.Interaction,
        username: str,
    ):
        # Komendy i tak są synchronizowane jako guild commands, ale zostawiamy
        # dodatkowe zabezpieczenie zgodne z config.json.
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        username = username.strip()

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
