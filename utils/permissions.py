import discord
from discord import app_commands
from config import UNRESTRICTED_ROLE_IDS, LEADER_ROLE_ID, DIVISIONS


def _role_ids(member: discord.Member) -> set[int]:
    return {r.id for r in member.roles}


def is_unrestricted(member: discord.Member) -> bool:
    """Captain or Mentor — full access with no restrictions."""
    return bool(_role_ids(member) & set(UNRESTRICTED_ROLE_IDS))


def is_leader(member: discord.Member) -> bool:
    """True if the member has the Leader role."""
    return LEADER_ROLE_ID in _role_ids(member)


def get_member_divisions(member: discord.Member) -> list[str]:
    """All division keys the member belongs to (student or leader)."""
    ids = _role_ids(member)
    return [key for key, div in DIVISIONS.items() if div["role_id"] and div["role_id"] in ids]


def get_member_division(member: discord.Member) -> str | None:
    """First division key for the member, or None. Use get_member_divisions for multi-division checks."""
    divs = get_member_divisions(member)
    return divs[0] if divs else None


def get_lead_divisions(member: discord.Member) -> list[str]:
    """All division keys this member leads (has Leader role + matching division role)."""
    if not is_leader(member):
        return []
    return get_member_divisions(member)


def get_lead_division(member: discord.Member) -> str | None:
    """First division key this member leads, or None. Use get_lead_divisions for multi-division checks."""
    divs = get_lead_divisions(member)
    return divs[0] if divs else None


def can_edit_freely(member: discord.Member, division: str | None = None) -> bool:
    """
    True if the member can apply edits directly without going through a request.
      - Captains / Mentors: always, regardless of division
      - Division leads: only for divisions they lead
    """
    if is_unrestricted(member):
        return True
    lead_divs = get_lead_divisions(member)
    if lead_divs:
        return division is None or division in lead_divs
    return False


def can_request_edit(member: discord.Member, division: str) -> bool:
    """
    True if the member is a student in the given division.
    Students have the division role but NOT the Leader role.
    """
    if is_leader(member):
        return False  # leaders edit directly, not via requests
    return division in get_member_divisions(member)


def captain_only() -> app_commands.check:
    """Restricts a slash command to captains and mentors (unrestricted roles)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_unrestricted(interaction.user)
    return app_commands.check(predicate)


async def find_division_lead(guild: discord.Guild, division: str) -> discord.Member | None:
    """
    Find a member who has BOTH the Leader role AND the given division role.
    That combination makes them the lead for that division.
    """
    leader_role   = guild.get_role(LEADER_ROLE_ID)
    division_role = guild.get_role(DIVISIONS[division]["role_id"])

    if not leader_role or not division_role:
        return None

    lead_ids = {m.id for m in leader_role.members} & {m.id for m in division_role.members}
    if not lead_ids:
        return None

    return guild.get_member(next(iter(lead_ids)))
