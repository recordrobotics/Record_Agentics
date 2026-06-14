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
from datetime import datetime as _dt

from config import DIVISIONS
from utils import channels
from utils.meetings import recent_past_meetings
from utils.permissions import (
    can_edit_freely, can_request_edit,
    get_member_division, get_lead_division,
    get_member_divisions, get_lead_divisions,
    find_division_lead, is_unrestricted, is_leader,
    captain_only,
)
from utils.store import (
    get_agenda_tasks, add_agenda_task,
    complete_task, delete_agenda_task,
    get_achievements, uncomplete_achievement,
    prune_achievements_before,
    create_request,
    resync as store_resync,
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

def _group_by_division(items: list[dict], text_key: str, bullet: str) -> list[str]:
    """Render a list of rows into division-grouped markdown lines."""
    groups: dict[str, list[str]] = {}
    for it in items:
        div = it.get("division", "general").lower()
        groups.setdefault(div, []).append(f"{bullet}  {it[text_key]}")

    lines: list[str] = []
    for key, div in DIVISIONS.items():
        if key in groups:
            lines.append(f"### {div['emoji']} {div['name']}")
            lines.extend(groups[key])
            lines.append("")
    if "general" in groups:
        lines.append("### 📌 General")
        lines.extend(groups["general"])
        lines.append("")
    return lines


def build_embed(agenda: list[dict], achievements: list[dict],
                m_last: "_dt | None") -> discord.Embed:
    lines: list[str] = []

    # ── Agenda ────────────────────────────────────────────────────────────────
    lines.append("## 📋  Agenda")
    agenda_lines = _group_by_division(agenda, "task", "•")
    if agenda_lines:
        lines.extend(agenda_lines)
    else:
        lines.append("*No tasks yet.*")
        lines.append("")

    lines.append("─" * 40)
    lines.append("")

    # ── Achievements (split into the two most recent meeting windows) ──────────
    lines.append("## 🏅  Achievements")

    since_last, previous = [], []
    for a in achievements:
        ts = str(a.get("achieved_at", "")).strip()
        completed_after_last = True
        if m_last and ts:
            try:
                completed_after_last = _dt.fromisoformat(ts) >= m_last
            except ValueError:
                completed_after_last = True
        (since_last if completed_after_last else previous).append(a)

    if not since_last and not previous:
        lines.append("*Nothing here yet — completed tasks appear here automatically.*")
    else:
        lines.append("### Since last meeting")
        body = _group_by_division(since_last, "achievement", "🏆")
        lines.extend(body if body else ["*— none —*", ""])
        if previous:
            lines.append("### Previous meeting")
            lines.extend(_group_by_division(previous, "achievement", "🏆"))

    description = "\n".join(lines)
    if len(description) > 4096:
        description = description[:4090] + "\n…"

    embed = discord.Embed(description=description, color=discord.Color.blurple())
    embed.set_footer(
        text="Completed tasks move to Achievements instantly  •  "
             "Achievements clear after 2 meetings  •  "
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

        self.all_tasks = _sort_by_division(tasks)[:25]
        if self.all_tasks:
            options = [
                discord.SelectOption(
                    label=t["task"][:100],
                    value=str(i),
                    description=DIVISIONS.get(t.get("division", ""), {}).get("name", "General"),
                )
                for i, t in enumerate(self.all_tasks)
            ]
            max_vals = min(len(options), 25)
        else:
            options = [discord.SelectOption(label="No tasks available", value="__none__")]
            max_vals = 1

        super().__init__(
            placeholder="Select tasks to move to Achievements…",
            options=options,
            min_values=0,
            max_values=max_vals,
        )

    async def callback(self, interaction: discord.Interaction):
        if "__none__" in self.values:
            await _temp_response(interaction, "No tasks available.")
            return

        await interaction.response.defer(ephemeral=True)
        selected = {int(v) for v in self.values}
        changed = 0
        for i, task in enumerate(self.all_tasks):
            if i in selected and complete_task(task["id"], editor=self.member.display_name):
                changed += 1

        if changed:
            await self.cog.refresh_panel()
        try:
            await self.orig_interaction.delete_original_response()
        except Exception:
            pass
        await _temp_followup(
            interaction,
            f"Moved {changed} task(s) to Achievements." if changed else "No changes made.",
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

        # All agenda tasks in scope — done ones pre-selected
        self.all_tasks = _sort_by_division(tasks)[:25]

        if self.all_tasks:
            options = [
                discord.SelectOption(
                    label=t["task"][:100],
                    value=str(i),
                    description=DIVISIONS.get(t.get("division", ""), {}).get("name", "General"),
                )
                for i, t in enumerate(self.all_tasks)
            ]
            max_vals = min(len(options), 25)
        else:
            options = [discord.SelectOption(label="No tasks available", value="__none__")]
            max_vals = 1

        super().__init__(
            placeholder="Select tasks to request moving to Achievements…",
            options=options,
            min_values=0,
            max_values=max_vals,
        )

    async def callback(self, interaction: discord.Interaction):
        if "__none__" in self.values:
            await _temp_response(interaction, "No tasks available.")
            return

        await interaction.response.defer(ephemeral=True)

        handler = self.cog.bot.get_cog("RequestHandler")
        if not handler:
            await _temp_followup(interaction, "Request system unavailable.")
            return

        selected = {int(v) for v in self.values}
        chosen = [task for i, task in enumerate(self.all_tasks) if i in selected]

        if not chosen:
            try:
                await self.orig_interaction.delete_original_response()
            except Exception:
                pass
            await _temp_followup(interaction, "No changes made.")
            return

        # Group chosen tasks by division so each goes to the right lead.
        by_division: dict[str, list[dict]] = {}
        for task in chosen:
            by_division.setdefault(task.get("division", "general").lower(), []).append(task)

        submitted = 0
        any_lead_found = False
        for division, tasks in by_division.items():
            lead = await find_division_lead(self.member.guild, division)
            if lead:
                any_lead_found = True
            for task in tasks:
                payload = {"task_id": task["id"], "task_name": task["task"]}
                request_id = create_request(
                    division, "agenda_complete", payload,
                    self.member.id, self.member.display_name,
                )
                req = {
                    "id": request_id, "division": division,
                    "action": "agenda_complete",
                    "payload": payload,
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


# ─── Undo: move an achievement back to the Agenda (leads/captains direct) ──────

def _ach_options(items: list[dict]) -> tuple[list[discord.SelectOption], int]:
    if items:
        options = [
            discord.SelectOption(
                label=a["achievement"][:100],
                value=str(i),
                description=DIVISIONS.get(a.get("division", ""), {}).get("name", "General"),
            )
            for i, a in enumerate(items)
        ]
        return options, min(len(options), 25)
    return [discord.SelectOption(label="No achievements available", value="__none__")], 1


class UndoAchievedSelect(discord.ui.Select):
    def __init__(self, achievements: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        self.cog = cog
        self.member = member
        self.orig_interaction = orig_interaction
        self.all_items = _sort_by_division(achievements)[:25]
        options, max_vals = _ach_options(self.all_items)
        super().__init__(
            placeholder="Select achievements to move back to Agenda…",
            options=options, min_values=0, max_values=max_vals,
        )

    async def callback(self, interaction: discord.Interaction):
        if "__none__" in self.values:
            await _temp_response(interaction, "No achievements available.")
            return
        await interaction.response.defer(ephemeral=True)
        selected = {int(v) for v in self.values}
        changed = 0
        for i, a in enumerate(self.all_items):
            if i in selected and uncomplete_achievement(a["id"], editor=self.member.display_name):
                changed += 1
        if changed:
            await self.cog.refresh_panel()
        try:
            await self.orig_interaction.delete_original_response()
        except Exception:
            pass
        await _temp_followup(
            interaction,
            f"Moved {changed} achievement(s) back to Agenda." if changed else "No changes made.",
        )


class UndoAchievedView(discord.ui.View):
    def __init__(self, achievements: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.add_item(UndoAchievedSelect(achievements, cog, member, orig_interaction))


# ─── Undo request (regular members) ───────────────────────────────────────────

class RequestUndoSelect(discord.ui.Select):
    def __init__(self, achievements: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        self.cog = cog
        self.member = member
        self.orig_interaction = orig_interaction
        self.all_items = _sort_by_division(achievements)[:25]
        options, max_vals = _ach_options(self.all_items)
        super().__init__(
            placeholder="Select achievements to request moving back…",
            options=options, min_values=0, max_values=max_vals,
        )

    async def callback(self, interaction: discord.Interaction):
        if "__none__" in self.values:
            await _temp_response(interaction, "No achievements available.")
            return
        await interaction.response.defer(ephemeral=True)

        handler = self.cog.bot.get_cog("RequestHandler")
        if not handler:
            await _temp_followup(interaction, "Request system unavailable.")
            return

        selected = {int(v) for v in self.values}
        chosen = [a for i, a in enumerate(self.all_items) if i in selected]
        if not chosen:
            try:
                await self.orig_interaction.delete_original_response()
            except Exception:
                pass
            await _temp_followup(interaction, "No changes made.")
            return

        by_division: dict[str, list[dict]] = {}
        for a in chosen:
            by_division.setdefault(a.get("division", "general").lower(), []).append(a)

        submitted = 0
        any_lead_found = False
        for division, achs in by_division.items():
            lead = await find_division_lead(self.member.guild, division)
            if lead:
                any_lead_found = True
            for a in achs:
                payload = {"ach_id": a["id"], "achievement_name": a["achievement"]}
                request_id = create_request(
                    division, "achievement_undo", payload,
                    self.member.id, self.member.display_name,
                )
                req = {
                    "id": request_id, "division": division,
                    "action": "achievement_undo", "payload": payload,
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
        label = f"{submitted} request(s)" if submitted > 1 else "Request"
        if any_lead_found:
            await _temp_followup(interaction, f"{label} submitted to division lead(s) — check your DMs for status.")
        else:
            await _temp_followup(interaction, f"{label} saved, but no division lead found. Ask a captain to follow up.")


class RequestUndoView(discord.ui.View):
    def __init__(self, achievements: list[dict], cog: "AgendaAchievementsPanel",
                 member: discord.Member, orig_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.add_item(RequestUndoSelect(achievements, cog, member, orig_interaction))


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
            await interaction.response.send_message(
                "Select tasks to move to Achievements:",
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
                "Select tasks to move to Achievements:",
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
                "Select tasks to request moving to Achievements:",
                view=RequestAchievedView(tasks, self.cog, member, interaction),
                ephemeral=True,
            )

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.danger, emoji="↩️", row=1)
    async def undo_achieved(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        all_ach = get_achievements()

        if is_unrestricted(member):
            await interaction.response.send_message(
                "Select achievements to move back to the Agenda:",
                view=UndoAchievedView(all_ach, self.cog, member, interaction),
                ephemeral=True,
            )
        elif is_leader(member):
            divs = get_lead_divisions(member)
            if not divs:
                await _temp_response(interaction, "You don't have a division role set.")
                return
            achs = [a for a in all_ach if a.get("division", "").lower() in divs]
            await interaction.response.send_message(
                "Select achievements to move back to the Agenda:",
                view=UndoAchievedView(achs, self.cog, member, interaction),
                ephemeral=True,
            )
        else:
            divs = get_member_divisions(member)
            if not divs:
                await _temp_response(interaction, "You don't have a division role set. Ask a captain to assign you one.")
                return
            achs = [a for a in all_ach if a.get("division", "").lower() in divs]
            await interaction.response.send_message(
                "Select achievements to request moving back to the Agenda:",
                view=RequestUndoView(achs, self.cog, member, interaction),
                ephemeral=True,
            )


# ─── Cog ──────────────────────────────────────────────────────────────────────

class AgendaAchievementsPanel(commands.Cog, name="AgendaAchievementsPanel"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_id = self._load_id()
        self.panel_refresh_loop.start()

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
        channel_id = channels.get_channel_id("agenda")
        channel = self.bot.get_channel(channel_id)
        if not channel:
            print(f"[AgendaAchievements] Channel {channel_id} not found.")
            return
        if viewer is None:
            viewer = channel.guild.me

        # Age out achievements that fell outside the 2-meeting window.
        meetings = recent_past_meetings()
        m_last = meetings[0] if meetings else None
        if len(meetings) >= 2:
            prune_achievements_before(meetings[1])

        agenda       = get_agenda_tasks()
        achievements = get_achievements()
        embed        = build_embed(agenda, achievements, m_last)
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

    # ── Auto refresh: pull sheet edits, then redraw the panel ──────────────────

    @tasks.loop(minutes=1)
    async def panel_refresh_loop(self):
        # Pull any manual spreadsheet edits (e.g. a row deleted by hand) back
        # into memory before redrawing, so they show up in the panel.
        try:
            await store_resync()
            await self.refresh_panel()
        except discord.Forbidden:
            print("[AgendaAchievements] Missing permission to post in the Agenda "
                  "channel — grant the bot View Channel / Send Messages / Embed Links.")
        except Exception as e:
            print(f"[AgendaAchievements] Refresh loop error: {e}")

    @panel_refresh_loop.before_loop
    async def before_panel_refresh(self):
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

        channel_id = channels.get_channel_id("agenda")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            await _temp_followup(
                interaction,
                f"I can't find the Agenda channel (id `{channel_id}`). Run `/set_channel` "
                f"in the channel you want this panel in, and make sure I can see it.",
            )
            return

        # Check my actual permissions IN that channel (channel overrides beat the
        # server-wide grant) and report exactly what's missing.
        me = channel.guild.me
        perms = channel.permissions_for(me)
        needed = {
            "View Channel": perms.view_channel,
            "Send Messages": perms.send_messages,
            "Embed Links": perms.embed_links,
            "Read Message History": perms.read_message_history,
        }
        missing = [name for name, ok in needed.items() if not ok]
        if missing:
            await _temp_followup(
                interaction,
                f"I'm missing **{', '.join(missing)}** in {channel.mention}.\n"
                f"Open that channel → Edit Channel → Permissions, add my role, and allow "
                f"those — channel permissions override the server-wide ones. Then run "
                f"`/setup_panel` again.",
            )
            return

        try:
            await self.refresh_panel(interaction.user)
            await _temp_followup(interaction, f"Panel is live in {channel.mention}!")
        except discord.Forbidden:
            await _temp_followup(
                interaction,
                f"Discord refused the post in {channel.mention} (Missing Access). "
                f"Double-check my channel permission overrides there, then retry.",
            )
        except Exception as e:
            await _temp_followup(interaction, f"Error: {e}")

    @setup_panel.error
    async def setup_panel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("This command is for captains and mentors only.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AgendaAchievementsPanel(bot))
