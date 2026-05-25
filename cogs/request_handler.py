"""
Handles the full request lifecycle:
  1. Bot DMs division lead with Approve / Deny buttons
  2. Lead clicks a button → lead's DM is deleted
  3. If approved → change applied + requester gets a result DM
  4. If denied   → requester gets a result DM with optional reason
  5. Result DM auto-deletes after RESULT_DM_DISPLAY_HOURS
"""

import asyncio
import discord
from discord.ext import commands
from config import DIVISIONS, RESULT_DM_DISPLAY_HOURS
from utils.sheets import (
    get_request, update_request_status,
    add_agenda_task, toggle_agenda_task,
    add_achievement,
)


async def _delete_after(msg: discord.Message, delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await msg.delete()
    except Exception:
        pass


# ─── Embed builder ────────────────────────────────────────────────────────────

def make_request_embed(req: dict, status: str = "pending") -> discord.Embed:
    div_name = DIVISIONS.get(req["division"], {}).get("name", req["division"])
    action_labels = {
        "agenda_add":      "Add Agenda Task",
        "agenda_toggle":   "Toggle Agenda Task",
        "achievement_add": "Add Achievement",
    }
    action_label = action_labels.get(req["action"], req["action"])

    color_map = {
        "pending":  discord.Color.orange(),
        "approved": discord.Color.green(),
        "denied":   discord.Color.red(),
    }

    embed = discord.Embed(
        title=f"Edit Request — {div_name}",
        color=color_map.get(status, discord.Color.greyple()),
    )
    embed.add_field(name="Action",   value=action_label,          inline=True)
    embed.add_field(name="Division", value=div_name,              inline=True)
    embed.add_field(name="From",     value=req["requester_name"], inline=True)

    payload = req.get("payload", {})
    if req["action"] in ("agenda_add", "achievement_add"):
        embed.add_field(name="Content", value=payload.get("text", "—"), inline=False)
    elif req["action"] == "agenda_toggle":
        new_state = "✅ Done" if payload.get("done") else "☐ Undone"
        embed.add_field(name="Task",      value=payload.get("task_name", "—"), inline=False)
        embed.add_field(name="New State", value=new_state,                     inline=True)

    status_label = {"pending": "⏳ Pending", "approved": "✅ Approved", "denied": "❌ Denied"}
    embed.set_footer(text=f"Request ID: {req['id']}  •  {status_label.get(status, status)}")
    return embed


# ─── Cog ──────────────────────────────────────────────────────────────────────

class RequestHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Maps request_id → requester's pending DM message (in-memory; lost on restart)
        self._pending_dms: dict[str, discord.Message] = {}

    # Listen for button clicks anywhere (including DMs)
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")

        if custom_id.startswith("req_approve_"):
            await self._handle_approve(interaction, custom_id[len("req_approve_"):])
        elif custom_id.startswith("req_deny_"):
            await self._handle_deny(interaction, custom_id[len("req_deny_"):])

    # ── Approve ───────────────────────────────────────────────────────────────

    async def _handle_approve(self, interaction: discord.Interaction, request_id: str):
        await interaction.response.defer()

        req = get_request(request_id)
        if not req:
            await interaction.followup.send("Request not found.")
            return
        if req.get("status") != "pending":
            await interaction.followup.send(f"Already **{req['status']}**.")
            return

        payload = req.get("payload", {})
        try:
            if req["action"] == "agenda_add":
                add_agenda_task(payload["text"], req["division"])
            elif req["action"] == "agenda_toggle":
                toggle_agenda_task(payload["task_name"], req["division"], payload["done"])
            elif req["action"] == "achievement_add":
                add_achievement(payload["text"], req["division"])
        except Exception as e:
            await interaction.followup.send(f"Failed to apply change: {e}")
            return

        update_request_status(request_id, "approved")

        # Delete the lead's pending DM and the requester's pending DM
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await self._delete_pending_dm(request_id)

        # Notify the requester (result DM auto-deletes after RESULT_DM_DISPLAY_HOURS)
        await self._notify_student(
            req,
            approved=True,
            message="Your edit request was **approved** and has been applied.",
        )

        await self._refresh_panel(req["action"])

    # ── Deny ──────────────────────────────────────────────────────────────────

    async def _handle_deny(self, interaction: discord.Interaction, request_id: str):
        req = get_request(request_id)
        if not req:
            await interaction.response.send_message("Request not found.")
            return
        if req.get("status") != "pending":
            await interaction.response.send_message(f"Already **{req['status']}**.")
            return

        modal = DenyReasonModal(request_id, req, self)
        await interaction.response.send_modal(modal)

    async def _finish_deny(self, interaction: discord.Interaction, req: dict, reason: str):
        update_request_status(req["id"], "denied")

        # Delete the lead's pending DM and the requester's pending DM
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await self._delete_pending_dm(req["id"])

        await self._notify_student(
            req,
            approved=False,
            message="Your edit request was **denied**." + (f"\n> {reason}" if reason else ""),
        )

        await interaction.followup.send("Request denied.", ephemeral=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _notify_student(self, req: dict, approved: bool, message: str):
        try:
            user = await self.bot.fetch_user(int(req["requester_id"]))
            div_name = DIVISIONS.get(req["division"], {}).get("name", req["division"])
            embed = discord.Embed(
                title=f"{'✅' if approved else '❌'} Request {'Approved' if approved else 'Denied'} — {div_name}",
                description=message,
                color=discord.Color.green() if approved else discord.Color.red(),
            )
            embed.set_footer(text=f"Request ID: {req['id']}")
            msg = await user.send(embed=embed)
            asyncio.create_task(_delete_after(msg, RESULT_DM_DISPLAY_HOURS * 3600))
        except Exception:
            pass

    async def _refresh_panel(self, action: str):
        cog = self.bot.get_cog("AgendaAchievementsPanel")
        if cog:
            await cog.refresh_panel()

    async def _delete_pending_dm(self, request_id: str):
        msg = self._pending_dms.pop(request_id, None)
        if msg:
            try:
                await msg.delete()
            except Exception:
                pass

    # ── Public helpers called by other cogs ───────────────────────────────────

    async def notify_requester_pending(self, member: discord.Member, req: dict):
        """Send requester a DM showing their request is pending. Stored so it can be deleted on resolution."""
        try:
            embed = make_request_embed(req, "pending")
            embed.description = "Your request is pending review by your division lead."
            msg = await member.send(embed=embed)
            self._pending_dms[req["id"]] = msg
        except Exception:
            pass

    async def send_request_dm(self, guild: discord.Guild, lead: discord.Member, req: dict):
        embed = make_request_embed(req, "pending")

        approve_btn = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"req_approve_{req['id']}",
        )
        deny_btn = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"req_deny_{req['id']}",
        )
        view = discord.ui.View(timeout=None)
        view.add_item(approve_btn)
        view.add_item(deny_btn)

        try:
            await lead.send(
                content="A team member is requesting an edit. Review it below:",
                embed=embed,
                view=view,
            )
        except discord.Forbidden:
            pass


# ─── Deny Reason Modal ────────────────────────────────────────────────────────

class DenyReasonModal(discord.ui.Modal, title="Deny Request"):
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        placeholder="Let the student know why...",
        required=False,
        max_length=300,
    )

    def __init__(self, request_id: str, req: dict, cog: RequestHandler):
        super().__init__()
        self.request_id = request_id
        self.req = req
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog._finish_deny(interaction, self.req, self.reason.value.strip())


async def setup(bot: commands.Bot):
    await bot.add_cog(RequestHandler(bot))
