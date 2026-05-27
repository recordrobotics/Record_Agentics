import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os

_TEMP_MSG_DELAY = 4


async def _temp_followup(interaction: discord.Interaction, content: str) -> None:
    msg = await interaction.followup.send(content, ephemeral=True)
    async def _delete():
        await asyncio.sleep(_TEMP_MSG_DELAY)
        try:
            await msg.delete()
        except Exception:
            pass
    asyncio.create_task(_delete())

from utils.gcal import get_upcoming_events, build_calendar_text
from utils.permissions import captain_only

MSG_ID_FILE = "calendar_msg_id.txt"


class CalendarPanel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("CALENDAR_CHANNEL_ID", 0))
        self.message_id = self._load_id()
        self.refresh_loop.start()

    def _load_id(self) -> int | None:
        try:
            with open(MSG_ID_FILE) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _save_id(self, msg_id: int):
        self.message_id = msg_id
        with open(MSG_ID_FILE, "w") as f:
            f.write(str(msg_id))

    def _build_embed(self) -> discord.Embed:
        events = get_upcoming_events(max_results=15)
        embed = discord.Embed(
            title="📆  Team Calendar",
            description=build_calendar_text(events),
            color=discord.Color.blue(),
        )
        embed.set_footer(
            text="Auto-refreshes every hour  •  "
                 "Tag events with [Division] in Google Calendar to group them here\n"
                 "Example: '[Programming] Code review' or '[Engineering] Build day'"
        )
        return embed

    async def _refresh(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            return

        embed = self._build_embed()

        if self.message_id:
            try:
                msg = await channel.fetch_message(self.message_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass

        msg = await channel.send(embed=embed)
        self._save_id(msg.id)

    @tasks.loop(hours=1)
    async def refresh_loop(self):
        await self._refresh()

    @refresh_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="calendar", description="Show the team calendar")
    async def show_calendar(self, interaction: discord.Interaction):
        embed = self._build_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="refresh_calendar", description="Force-refresh the calendar panel")
    @captain_only()
    async def force_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._refresh()
        await _temp_followup(interaction, "Calendar refreshed!")

    @force_refresh.error
    async def force_refresh_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CalendarPanel(bot))
