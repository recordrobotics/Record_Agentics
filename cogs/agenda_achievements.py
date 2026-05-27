"""
Unified Agenda + Achievements panel.

Task lifecycle:
  1. Active: shown in Agenda
  2. Marked done: shown crossed-off in Agenda for DONE_TO_ACHIEVEMENT_HOURS
  3. After DONE_TO_ACHIEVEMENT_HOURS: moves to Achievements section
  4. After ACHIEVEMENT_DISPLAY_HOURS: removed from Achievements entirely
"""

import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone
import os

from config import DIVISIONS, DONE_TO_ACHIEVEMENT_HOURS, ACHIEVEMENT_DISPLAY_HOURS, AGENDA_ACHIEVEMENTS_CHANNEL_ID
from utils.permissions import (
    can_edit_freely, can_request_edit,
    get_member_division, get_lead_division,
    get_member_divisions, get_lead_divisions,
    find_division_lead, is_unrestricted, is_leader,
    captain_only,
)
from utils.sheets import (
    get_agenda_tasks, add_agenda_task,
    toggle_agenda_task, delete_agenda_task,
    get_achievements,
    move_done_tasks_to_achievements,
    expire_old_achievements,
    create_request,
)

MSG_ID_FILE = "agenda_achievements_msg_id.txt"

_TEMP_MSG_DELAY = 4  # seconds before ephemeral confirmations vanish

# Division display order (matches agenda panel order)
_DIV_ORDER = {key: i for i, key in enumerate(DIVISIONS.keys())}


def _sort_by_division(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda t: _DIV_ORDER.get(t.get("division", "").lower(), 999))


async def _temp_followup(interaction: discord.Interaction, content: str) -> None:
    msg = await interaction.followup.send(content, ephemeral=True)
    async def _delete():
        await asyncio.sleep(_TEMP_MSG_DELAY)
        try:
            await msg.delete()
        except Exception:
            pass
    asyncio.create_task(_delete())


async def _temp_response(interaction: discord.Interaction, content: str) -> None:
    await interaction.response.send_message(content, ephemeral=True)
    async def _delete():
        await asyncio.sleep(_TEMP_MSG_DELAY)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
    asyncio.create_task(_delete())


# ─── Embed builder ────────────────────────────────────────────────────────────

def _time_left_label(done_at_str: str) -> str:
    try:
        done_at = datetime.fromisoformat(done_at_str)
        now = datetime.now(timezone.utc)
        remaining = DONE_TO_ACHIEVEMENT_HOURS - (now - done_at).total_seconds() / 3600
        if remaining <= 1:
            return "moves soon"
        elif remaining < 24:
            return f"moves in {int(remaining)}h"
        else:
            return f"moves in {int(remaining / 24)}d"
    except Exception:
        return ""


def build_embed(agenda: list[dict], achievements: list[dict]) -> discord.Embed:
    lines: list[str] = []

    # ── Agenda ────────────────────────────────────────────────────────────────
    lines.append("## 📋  Agenda")

    agenda_groups: dict[str, list[str]] = {}
    for t in agenda:
        div = t.get("division", "general").lower()
        done = str(t.get("done", "FALSE")).upper() == "TRUE"
        if done:
            done_at = str(t.get("done_at", "")).strip()
            hint = f"  *({_time_left_label(done_at)})*" if done_at else ""
            line = f"~~{t['task']}~~{hint}"
        else:
            line = f"• {t['task']}"
        agenda_groups.setdefault(div, []).append(line)

    if agenda_groups:
        for key, div in DIVISIONS.items():
            if key in agenda_groups:
                lines.append(f"### {div['emoji']} {div['name']}")
                lines.extend(agenda_groups[key])
                lines.append("")
        if "general" in agenda_groups:
            lines.append("### 📌 General")
            lines.extend(agenda_groups["general"])
            lines.append("")
    else:
        lines.append("*No tasks yet.*")
        lines.append("")

    lines.append("─" * 40)
    lines.append("")

    # ── Achievements ──────────────────────────────────────────────────────────
    lines.append("## 🏅  Achievements")

    ach_groups: dict[str, list[str]] = {}
    for a in achievements:
        div = a.get("division", "general").lower()
        ach_groups.setdefault(div, []).append(f"🏆  {a['achievement']}")

    if ach_groups:
        for key, div in DIVISIONS.items():
            if key in ach_groups:
                lines.append(f"### {div['emoji']} {div['name']}")
                lines.extend(ach_groups[key])
                lines.append("")
        if "general" in ach_groups:
            lines.append("### 📌 General")
            lines.extend(ach_groups["general"])
            lines.append("")
    else:
        lines.append("*Nothing here yet — completed tasks appear here automatically.*")

    description = "\n".join(lines)
    if len(description) > 4096:
        description = description[:4090] + "\n…"

    embed = discord.Embed(description=description, color=discord.Color.blurple())
    embed.set_footer(
        text=f"Crossed-off → Achievements after {DONE_TO_ACHIEVEMENT_HOURS}h  •  "
             f"Achievements hidden after {ACHIEVEMENT_DISPLAY_HOURS}h  •  "
             "Leads edit directly  •  Members submit a request"
    )
    return embed


# ─── Modals ───────────────────────────────────────────────────────────────────

class AddTaskModal(discord.ui.Modal, title="Add Task"):
    task_input = discord.ui.TextInput(
        label="Task description",
        placeholder="e.g. Finish wiring | Write documentation | Test intake",
        max_length=500,
    )

    def __init__(self, cog: "AgendaAchievementsPanel", member: discord.Member,
                 division_override: str | None = None):
        super().__init__()
        self.cog = cog
        self.member = member
        self.division_override = division_override

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tasks = [t.strip() for t in self.task_input.value.split("|") if t.strip()]
        if not tasks:
            await _temp_followup(interaction, "No task text provided.")
            return
        division = self.division_override or get_lead_division(self.member) or get_member_division(self.member)

        if can_edit_freely(self.member, division):
            for text in tasks:
                add_agenda_task(text, division or "general", editor=self.member.display_name)
            await self.cog.refresh_panel()
            div_name = DIVISIONS.get(division or "", {}).get("name", "General")
            label = f"{len(tasks)} tasks" if len(tasks) > 1 else "Task"
            await _temp_followup(interaction, f"{label} added to **{div_name}**.")
        elif division and can_request_edit(self.member, division):
            for text in tasks:
                await self.cog._submit_request(
                    interaction, self.member, division, "agenda_add", {"text": text}
                )
        else:
            await _temp_followup(
                interaction,
                "You don't have a division role set. Ask a captain to assign you one.",
            )


# ─── Mark as achieved (multi-select, leads/captains only) ────────────────────

class MarkAchievedSelect(discord.ui.Select):
    def __init__(self, tasks: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        self.cog = cog
        self.member = member
        self.orig_interaction = orig_interaction

        pending = [t for t in tasks if str(t.get("done", "FALSE")).upper() != "TRUE"]
        self.eligible = _sort_by_division(pending)[:25]
        if self.eligible:
            options = [
                discord.SelectOption(
                    label=t["task"][:100],
                    value=str(i),
                    description=DIVISIONS.get(t.get("division", ""), {}).get("name", "General"),
                )
                for i, t in enumerate(self.eligible)
            ]
            max_vals = min(len(options), 25)
        else:
            options = [discord.SelectOption(label="No pending tasks available", value="__none__")]
            max_vals = 1

        super().__init__(
            placeholder="Select tasks to mark as done…",
            options=options,
            min_values=1,
            max_values=max_vals,
        )

    async def callback(self, interaction: discord.Interaction):
        if "__none__" in self.values:
            await _temp_response(interaction, "No pending tasks available.")
            return

        await interaction.response.defer(ephemeral=True)
        marked = 0
        for idx in self.values:
            task = self.eligible[int(idx)]
            toggle_agenda_task(task["task"], task.get("division", "general"), True,
                               editor=self.member.display_name)
            marked += 1

        if marked:
            await self.cog.refresh_panel()
        try:
            await self.orig_interaction.delete_original_response()
        except Exception:
            pass
        await _temp_followup(
            interaction,
            f"Marked {marked} task(s) as done — moves to Achievements in {DONE_TO_ACHIEVEMENT_HOURS}h.",
        )


class MarkAchievedView(discord.ui.View):
    def __init__(self, tasks: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.add_item(MarkAchievedSelect(tasks, cog, member, orig_interaction))


# ─── Request to mark as achieved (multi-select, regular members) ──────────────

class RequestAchievedSelect(discord.ui.Select):
    def __init__(self, tasks: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        self.cog = cog
        self.member = member
        self.orig_interaction = orig_interaction

        pending = [t for t in tasks if str(t.get("done", "FALSE")).upper() != "TRUE"]
        self.eligible = _sort_by_division(pending)[:25]

        if self.eligible:
            options = [
                discord.SelectOption(
                    label=t["task"][:100],
                    value=str(i),
                    description=DIVISIONS.get(t.get("division", ""), {}).get("name", "General"),
                )
                for i, t in enumerate(self.eligible)
            ]
            max_vals = min(len(options), 25)
        else:
            options = [discord.SelectOption(label="No pending tasks available", value="__none__")]
            max_vals = 1

        super().__init__(
            placeholder="Select tasks to request marking as done…",
            options=options,
            min_values=1,
            max_values=max_vals,
        )

    async def callback(self, interaction: discord.Interaction):
        if "__none__" in self.values:
            await _temp_response(interaction, "No pending tasks available.")
            return

        await interaction.response.defer(ephemeral=True)

        handler = self.cog.bot.get_cog("RequestHandler")
        if not handler:
            await _temp_followup(interaction, "Request system unavailable.")
            return

        # Group selected tasks by division so we only DM each lead once per division
        by_division: dict[str, list[dict]] = {}
        for idx in self.values:
            task = self.eligible[int(idx)]
            div = task.get("division", "general").lower()
            by_division.setdefault(div, []).append(task)

        submitted = 0
        any_lead_found = False
        for division, div_tasks in by_division.items():
            lead = await find_division_lead(self.member.guild, division)
            if lead:
                any_lead_found = True
            for task in div_tasks:
                request_id = create_request(
                    division, "agenda_toggle",
                    {"task_name": task["task"], "done": True},
                    self.member.id, self.member.display_name,
                )
                req = {
                    "id": request_id, "division": division,
                    "action": "agenda_toggle",
                    "payload": {"task_name": task["task"], "done": True},
                    "requester_name": self.member.display_name,
                    "requester_id": self.member.id, "status": "pending",
                }
                if lead:
                    await handler.send_request_dm(self.member.guild, lead, req)
                await handler.notify_requester_pending(self.member, req)
                submitted += 1

        try:
            await self.orig_interaction.delete_original_response()
        except Exception:
            pass

        if submitted:
            label = f"{submitted} request(s)" if submitted > 1 else "Request"
            if any_lead_found:
                await _temp_followup(interaction, f"{label} submitted to division lead(s) — check your DMs for status.")
            else:
                await _temp_followup(interaction, f"{label} saved, but no division lead found. Ask a captain to follow up.")


class RequestAchievedView(discord.ui.View):
    def __init__(self, tasks: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.add_item(RequestAchievedSelect(tasks, cog, member, orig_interaction))


# ─── Division picker (select → opens modal) ───────────────────────────────────

class DivisionPickerSelect(discord.ui.Select):
    def __init__(self, cog: "AgendaAchievementsPanel", member: discord.Member,
                 orig_interaction: discord.Interaction,
                 allowed_keys: list[str] | None = None):
        self.cog = cog
        self.member = member
        self.orig_interaction = orig_interaction

        all_options = [
            discord.SelectOption(label=div["name"], value=key, emoji=div["emoji"])
            for key, div in DIVISIONS.items()
        ]
        if allowed_keys is None:
            # Unrestricted: all divisions + General
            all_options.append(discord.SelectOption(label="General", value="general", emoji="📌"))
            options = all_options
        else:
            options = [o for o in all_options if o.value in allowed_keys]

        super().__init__(placeholder="Select a division…", options=options)

    async def callback(self, interaction: discord.Interaction):
        modal = AddTaskModal(self.cog, self.member, division_override=self.values[0])
        await interaction.response.send_modal(modal)
        try:
            await self.orig_interaction.delete_original_response()
        except Exception:
            pass


class DivisionPickerView(discord.ui.View):
    def __init__(self, cog: "AgendaAchievementsPanel", member: discord.Member,
                 orig_interaction: discord.Interaction,
                 allowed_keys: list[str] | None = None):
        super().__init__(timeout=60)
        self.add_item(DivisionPickerSelect(cog, member, orig_interaction, allowed_keys=allowed_keys))


# ─── View ─────────────────────────────────────────────────────────────────────

class AgendaAchievementsView(discord.ui.View):
    def __init__(self, tasks: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Add Task", style=discord.ButtonStyle.primary, emoji="➕", row=1)
    async def add_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if is_unrestricted(member):
            # All divisions + General
            await interaction.response.send_message(
                "Which division is this task for?",
                view=DivisionPickerView(self.cog, member, interaction),
                ephemeral=True,
            )
        elif is_leader(member):
            divs = get_lead_divisions(member)
            if not divs:
                await interaction.response.send_modal(AddTaskModal(self.cog, member))
            elif len(divs) == 1:
                await interaction.response.send_modal(
                    AddTaskModal(self.cog, member, division_override=divs[0])
                )
            else:
                await interaction.response.send_message(
                    "Which division is this task for?",
                    view=DivisionPickerView(self.cog, member, interaction, allowed_keys=divs),
                    ephemeral=True,
                )
        else:
            divs = get_member_divisions(member)
            if not divs:
                await interaction.response.send_modal(AddTaskModal(self.cog, member))
            elif len(divs) == 1:
                await interaction.response.send_modal(
                    AddTaskModal(self.cog, member, division_override=divs[0])
                )
            else:
                await interaction.response.send_message(
                    "Which division is this task for?",
                    view=DivisionPickerView(self.cog, member, interaction, allowed_keys=divs),
                    ephemeral=True,
                )

    @discord.ui.button(label="Move to Achieved", style=discord.ButtonStyle.success, emoji="⭐", row=1)
    async def move_to_achieved(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        all_tasks = get_agenda_tasks()

        if is_unrestricted(member):
            # All tasks across all divisions
            await interaction.response.send_message(
                "Select tasks to mark as done:",
                view=MarkAchievedView(all_tasks, self.cog, member, interaction),
                ephemeral=True,
            )
        elif is_leader(member):
            divs = get_lead_divisions(member)
            if not divs:
                await _temp_response(interaction, "You don't have a division role set.")
                return
            tasks = [t for t in all_tasks if t.get("division", "").lower() in divs]
            await interaction.response.send_message(
                "Select tasks to mark as done:",
                view=MarkAchievedView(tasks, self.cog, member, interaction),
                ephemeral=True,
            )
        else:
            divs = get_member_divisions(member)
            if not divs:
                await _temp_response(interaction, "You don't have a division role set. Ask a captain to assign you one.")
                return
            tasks = [t for t in all_tasks if t.get("division", "").lower() in divs]
            await interaction.response.send_message(
                "Select tasks to request marking as done:",
                view=RequestAchievedView(tasks, self.cog, member, interaction),
                ephemeral=True,
            )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.refresh_panel(interaction.user)
        await _temp_followup(interaction, "Refreshed.")


# ─── Cog ──────────────────────────────────────────────────────────────────────

class AgendaAchievementsPanel(commands.Cog, name="AgendaAchievementsPanel"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = AGENDA_ACHIEVEMENTS_CHANNEL_ID
        self.message_id = self._load_id()
        self.auto_move_loop.start()

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

    async def refresh_panel(self, viewer: discord.Member | None = None):
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            print(f"[AgendaAchievements] Channel {self.channel_id} not found.")
            return
        if viewer is None:
            viewer = channel.guild.me

        agenda       = get_agenda_tasks()
        achievements = get_achievements()
        embed        = build_embed(agenda, achievements)
        view         = AgendaAchievementsView(agenda, self, viewer)

        if self.message_id:
            try:
                msg = await channel.fetch_message(self.message_id)
                await msg.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                pass

        msg = await channel.send(embed=embed, view=view)
        self._save_id(msg.id)

    # ── Hourly loop: move done tasks, expire old achievements, refresh ─────────

    @tasks.loop(hours=1)
    async def auto_move_loop(self):
        await self.bot.wait_until_ready()
        moved   = move_done_tasks_to_achievements(DONE_TO_ACHIEVEMENT_HOURS)
        expired = expire_old_achievements(ACHIEVEMENT_DISPLAY_HOURS)
        if moved or expired:
            print(f"[AgendaAchievements] Moved {moved} task(s), expired {expired} achievement(s).")
        await self.refresh_panel()

    @auto_move_loop.before_loop
    async def before_auto_move(self):
        await self.bot.wait_until_ready()

    # ── Request helper ────────────────────────────────────────────────────────

    async def _submit_request(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        division: str,
        action: str,
        payload: dict,
    ):
        handler = self.bot.get_cog("RequestHandler")
        if not handler:
            await _temp_followup(interaction, "Request system unavailable.")
            return

        request_id = create_request(division, action, payload, member.id, member.display_name)
        req = {
            "id": request_id, "division": division, "action": action,
            "payload": payload, "requester_name": member.display_name,
            "requester_id": member.id, "status": "pending",
        }

        lead = await find_division_lead(member.guild, division)
        if lead:
            await handler.send_request_dm(member.guild, lead, req)
            await handler.notify_requester_pending(member, req)
            div_name = DIVISIONS[division]["name"]
            await _temp_followup(
                interaction,
                f"Request submitted to **{div_name}** lead — check your DMs for status.",
            )
        else:
            await _temp_followup(
                interaction,
                "Request saved, but no division lead was found. Ask a captain to follow up.",
            )

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(
        name="setup_panel",
        description="Post or refresh the Agenda + Achievements panel",
    )
    @captain_only()
    async def setup_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.refresh_panel(interaction.user)
            await _temp_followup(interaction, "Panel is live!")
        except Exception as e:
            await _temp_followup(interaction, f"Error: {e}")

    @setup_panel.error
    async def setup_panel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AgendaAchievementsPanel(bot))
