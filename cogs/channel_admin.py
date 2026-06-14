"""
/set_channel — lets a captain assign a bot function to the channel they run it in.

The captain picks from a dropdown (Agenda & Achievements, Auto Polls & Meetings,
Calendar); the choice is saved to channel_config.json (via utils.channels) so it
sticks across restarts. The matching panel is refreshed into the new channel
right away where applicable.
"""

import discord
from discord.ext import commands
from discord import app_commands

from utils import channels
from utils.permissions import captain_only


class FunctionSelect(discord.ui.Select):
    def __init__(self, channel_id: int, orig_interaction: discord.Interaction):
        self.channel_id = channel_id
        self.orig_interaction = orig_interaction
        options = [
            discord.SelectOption(label=meta["label"], value=key)
            for key, meta in channels.FUNCTIONS.items()
        ]
        super().__init__(placeholder="Pick the function for this channel…", options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        channels.set_channel(key, self.channel_id)
        label = channels.FUNCTIONS[key]["label"]

        await interaction.response.send_message(
            f"✅ This channel is now the **{label}** channel.", ephemeral=True
        )

        # Move/repost the relevant panel into the new channel where it makes sense.
        if key == "agenda":
            cog = interaction.client.get_cog("AgendaAchievementsPanel")
            if cog:
                await cog.refresh_panel()

        try:
            await self.orig_interaction.delete_original_response()
        except Exception:
            pass


class FunctionSelectView(discord.ui.View):
    def __init__(self, channel_id: int, orig_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.add_item(FunctionSelect(channel_id, orig_interaction))


class ChannelAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="set_channel",
        description="Assign which bot function lives in this channel",
    )
    @captain_only()
    async def set_channel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which function should live in this channel?",
            view=FunctionSelectView(interaction.channel_id, interaction),
            ephemeral=True,
        )

    @set_channel.error
    async def set_channel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "This command is for captains and mentors only.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelAdmin(bot))
