import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands

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
from utils import channels

MSG_ID_FILE = "calendar_msg_id.txt"


class CalendarPanel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_id = self._load_id()
        self._perms_warned = False
        self.refresh_loop.start()

    @staticmethod
    def _missing_channel_perms(channel) -> list[str]:
        """Permissions the bot still needs in `channel` to post the calendar panel.

        Channel-level overrides beat the server-wide grant, so this checks the
        effective permissions in that specific channel.
        """
        perms = channel.permissions_for(channel.guild.me)
        needed = {
            "View Channel": perms.view_channel,
            "Send Messages": perms.send_messages,
            "Embed Links": perms.embed_links,
            "Read Message History": perms.read_message_history,
        }
        return [name for name, ok in needed.items() if not ok]

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
        events = get_upcoming_events(max_results=50)
        description = build_calendar_text(events)
        if len(description) > 4096:
            description = description[:4090] + "\n…"
        embed = discord.Embed(
            title="📆  Team Calendar",
            description=description,
            color=discord.Color.blue(),
        )
        embed.set_footer(
            text="Auto-refreshes every minute  •  "
                 "Tag events with [Division] in Google Calendar to group them here\n"
                 "Example: '[Programming] Code review' or '[Engineering] Build day'"
        )
        return embed

    async def _refresh(self) -> bool:
        """Redraw the calendar panel. Returns True if drawn, False if it couldn't
        (no channel set, or the bot lacks permission to post there)."""
        await self.bot.wait_until_ready()
        channel_id = channels.get_channel_id("calendar")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not channel:
            return False

        # Bail out cleanly (and quietly) if the bot can't post here, instead of
        # letting channel.send raise Forbidden every loop tick. Log only on the
        # transition so a missing grant doesn't spam the console each minute, and
        # announce recovery so it's clear when it resumes on its own.
        missing = self._missing_channel_perms(channel)
        if missing:
            if not self._perms_warned:
                print(f"[Calendar] Skipping panel refresh — missing "
                      f"{', '.join(missing)} in #{channel.name}. Grant these in the "
                      f"channel's permission overrides; I'll resume automatically.")
                self._perms_warned = True
            return False
        if self._perms_warned:
            print("[Calendar] Channel permissions restored — resuming panel refresh.")
            self._perms_warned = False

        embed = self._build_embed()

        if self.message_id:
            try:
                msg = await channel.fetch_message(self.message_id)
                await msg.edit(embed=embed)
                return True
            except discord.NotFound:
                pass

        msg = await channel.send(embed=embed)
        self._save_id(msg.id)
        return True

    @tasks.loop(minutes=1)
    async def refresh_loop(self):
        try:
            await self._refresh()
        except discord.Forbidden:
            # Backstop: perms revoked between the pre-check and the send.
            pass
        except Exception as e:
            print(f"[Calendar] Refresh loop error: {e}")

    @refresh_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="calendar", description="Show the team calendar")
    async def show_calendar(self, interaction: discord.Interaction):
        embed = self._build_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setup_calendar",
        description="Post or refresh the Team Calendar panel",
    )
    @captain_only()
    async def setup_calendar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        channel_id = channels.get_channel_id("calendar")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            await _temp_followup(
                interaction,
                f"I can't find the Calendar channel (id `{channel_id}`). Run `/set_channel` "
                f"in the channel you want this panel in, and make sure I can see it.",
            )
            return

        # Report exactly what's missing before attempting to post.
        missing = self._missing_channel_perms(channel)
        if missing:
            await _temp_followup(
                interaction,
                f"I'm missing **{', '.join(missing)}** in {channel.mention}.\n"
                f"Open that channel → Edit Channel → Permissions, add my role, and allow "
                f"those — channel permissions override the server-wide ones. Then run "
                f"`/setup_calendar` again.",
            )
            return

        try:
            await self._refresh()
            await _temp_followup(interaction, f"Calendar panel is live in {channel.mention}!")
        except discord.Forbidden:
            await _temp_followup(
                interaction,
                f"Discord refused the post in {channel.mention} (Missing Access). "
                f"Double-check my channel permission overrides there, then retry.",
            )
        except Exception as e:
            await _temp_followup(interaction, f"Error: {e}")

    @setup_calendar.error
    async def setup_calendar_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)

    @app_commands.command(name="refresh_calendar", description="Force-refresh the calendar panel")
    @captain_only()
    async def force_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await self._refresh():
            await _temp_followup(interaction, "Calendar refreshed!")
        else:
            await _temp_followup(
                interaction,
                "Couldn't refresh — set the channel with `/set_channel` and make sure "
                "I have permission to post there (try `/setup_calendar`).",
            )

    @force_refresh.error
    async def force_refresh_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CalendarPanel(bot))
