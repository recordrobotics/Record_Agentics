import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import os

from config import (
    SIGNUPS_CHANNEL_ID,
    SIGNUP_WEEKDAY,
    SIGNUP_HOUR,
    MEETING_DAYS,
    POLL_DURATION_HOURS,
)
from utils.permissions import captain_only

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

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


def _next_occurrence(from_date: datetime.date, weekday: int) -> datetime.date:
    """Next date on or after from_date that falls on `weekday` (0=Mon, 6=Sun)."""
    days = (weekday - from_date.weekday()) % 7
    if days == 0:
        days = 7  # always get a future/same-week date, not today
    return from_date + datetime.timedelta(days=days)


class Signups(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = SIGNUPS_CHANNEL_ID
        self._last_posted_week: int | None = None
        self.weekly_check.start()

    async def _do_post(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            print(f"[Signups] Channel {self.channel_id} not found.")
            return

        today = datetime.date.today()
        options = []
        for m in MEETING_DAYS:
            date = _next_occurrence(today, m["weekday"])
            label = f"{_WEEKDAY_NAMES[m['weekday']]}, {date.month}/{date.day}, from {m['time']}"
            options.append((date, label))

        options.sort(key=lambda x: x[0])
        first_date = options[0][0] if options else today
        question = f"Signups week {first_date.strftime('%b %-d')}"

        poll = discord.Poll(
            question=question,
            duration=datetime.timedelta(hours=POLL_DURATION_HOURS),
            multiple=True,
        )
        for _, label in options:
            poll.add_answer(text=label)

        await channel.send(poll=poll)

    @tasks.loop(minutes=30)
    async def weekly_check(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now()
        current_week = datetime.date.today().isocalendar().week
        if (
            now.weekday() == SIGNUP_WEEKDAY
            and now.hour == SIGNUP_HOUR
            and self._last_posted_week != current_week
        ):
            await self._do_post()
            self._last_posted_week = current_week

    @weekly_check.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="post_signup", description="Manually post this week's attendance poll")
    @captain_only()
    async def manual_post(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._do_post()
        await _temp_followup(interaction, "Poll posted!")

    @manual_post.error
    async def manual_post_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Signups(bot))
