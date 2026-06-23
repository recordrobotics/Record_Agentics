import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import json
import random
from zoneinfo import ZoneInfo

from config import (
    SIGNUP_WEEKDAY,
    SIGNUP_HOUR,
    MEETING_DAYS,
    POLL_DURATION_HOURS,
    STUDENTS_ROLE_ID,
    TIMEZONE,
)
from utils import channels, store
from utils.meetings import resolve_meeting_datetime, format_meeting_label
from utils.permissions import captain_only

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_TEMP_MSG_DELAY = 4

# Built-in emojis used for the "react if you're available" prompt on manual
# meetings. One is picked at random each time (never the same as last time).
_MEETING_EMOJIS = ["✅", "👍", "🙌", "🤖", "🦾", "⚡", "🔧", "🛠️", "🚀", "💪", "🔥", "⭐", "🎯", "🤝", "📌", "🗓️"]

# Ping the @Students role without accidentally pinging @everyone.
_PING_STUDENTS = discord.AllowedMentions(roles=True, everyone=False, users=False)

# Persisted signup state, survives restarts/redeploys:
#   last_posted_week — ISO week ("YYYY-WW") we last auto-posted, so a restart
#                      after the scheduled time doesn't re-post the same week.
#   auto_enabled     — whether the weekly auto-poll is on (toggled by command).
_STATE_FILE = "signup_state.json"


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[Signups] Could not save {_STATE_FILE}: {e}")


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


def _students_mention(guild: discord.Guild) -> str:
    role = guild.get_role(STUDENTS_ROLE_ID) if guild else None
    return role.mention if role else "@students"


class Signups(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        state = _load_state()
        self._last_posted_week: str | None = state.get("last_posted_week")
        self._auto_enabled: bool = state.get("auto_enabled", True)
        self._last_emoji: str | None = None
        self.weekly_check.start()

    def _persist(self) -> None:
        _save_state({
            "last_posted_week": self._last_posted_week,
            "auto_enabled": self._auto_enabled,
        })

    def _pick_emoji(self) -> str:
        choices = [e for e in _MEETING_EMOJIS if e != self._last_emoji] or _MEETING_EMOJIS
        self._last_emoji = random.choice(choices)
        return self._last_emoji

    async def _do_post(self):
        channel = self.bot.get_channel(channels.get_channel_id("signups"))
        if not channel:
            print("[Signups] Signups channel not set.")
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

        # Follow-up ping so students know the poll is up.
        await channel.send(
            f"{_students_mention(channel.guild)} 🗳️ Next week's attendance polls are up — please vote!",
            allowed_mentions=_PING_STUDENTS,
        )

    @tasks.loop(minutes=30)
    async def weekly_check(self):
        if not self._auto_enabled:
            return
        # Interpret the schedule in the team's local timezone — on Railway the
        # process clock is UTC, so a naive now() would never match local time.
        now = datetime.datetime.now(ZoneInfo(TIMEZONE))
        iso = now.isocalendar()
        current_week = f"{iso.year}-{iso.week:02d}"
        if self._last_posted_week == current_week:
            return
        # Post once we've reached the scheduled day/hour this week. Using "at or
        # past" (not "exactly at") means a tick missed during a restart/redeploy
        # still posts as soon as the bot is back, instead of skipping the week.
        reached = now.weekday() > SIGNUP_WEEKDAY or (
            now.weekday() == SIGNUP_WEEKDAY and now.hour >= SIGNUP_HOUR
        )
        if reached:
            await self._do_post()
            self._last_posted_week = current_week
            self._persist()

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

    @app_commands.command(
        name="auto_signup",
        description="Turn the weekly auto-poll on or off",
    )
    @app_commands.describe(enabled="True to enable the weekly auto-poll, False to disable it")
    @captain_only()
    async def auto_signup(self, interaction: discord.Interaction, enabled: bool):
        await interaction.response.defer(ephemeral=True)
        self._auto_enabled = enabled
        self._persist()
        state = "enabled ✅" if enabled else "disabled 🛑"
        await _temp_followup(interaction, f"Weekly auto-poll is now **{state}**.")

    @auto_signup.error
    async def auto_signup_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)

    @app_commands.command(
        name="add_meeting",
        description="Announce an extra (non-regular) meeting and ping students — no poll",
    )
    @app_commands.describe(
        day="Weekday (e.g. Friday) or date (e.g. 2026-06-20 or 6/20)",
        time="Time interval, e.g. 7-9 PM",
        message="What the meeting is about",
    )
    @captain_only()
    async def add_meeting(self, interaction: discord.Interaction, day: str, time: str, message: str):
        await interaction.response.defer(ephemeral=True)

        dt = resolve_meeting_datetime(day, time)
        if not dt:
            await _temp_followup(
                interaction,
                "Couldn't read that day/time. Try a day like `Friday` or a date like "
                "`2026-06-20`, plus a time like `7-9 PM`.",
            )
            return

        channel = self.bot.get_channel(channels.get_channel_id("signups"))
        if not channel:
            await _temp_followup(
                interaction,
                "No meetings channel is set. Run `/set_channel` in the channel you want first.",
            )
            return

        label = format_meeting_label(dt, time)
        emoji = self._pick_emoji()
        content = (
            f"{_students_mention(channel.guild)}\n\n"
            f"📣 **Extra Meeting — {label}**\n{message}\n\n"
            f"React {emoji} if you're available!"
        )
        msg = await channel.send(content, allowed_mentions=_PING_STUDENTS)
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

        # Record it so it counts toward the achievement meeting-window.
        store.add_meeting(dt.isoformat(), label, "manual")
        await _temp_followup(interaction, f"Meeting announced in {channel.mention} and added to the schedule.")

    @add_meeting.error
    async def add_meeting_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Signups(bot))
