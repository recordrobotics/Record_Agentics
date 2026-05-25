import discord
from config import UNRESTRICTED_ROLE_IDS, LEADER_ROLE_ID, DIVISIONS


def _role_ids(member: discord.Member) -> set[int]:
    return {r.id for r in member.roles}


def is_unrestricted(member: discord.Member) -> bool:
    """Captain or Mentor — full access with no restrictions."""
    return bool(_role_ids(member) & set(UNRESTRICTED_ROLE_IDS))


def is_leader(member: discord.Member) -> bool:
    """True if the member has the Leader role."""
    return LEADER_ROLE_ID in _role_ids(member)


def get_member_division(member: discord.Member) -> str | None:
    """
    Returns the division key for the division role the member has.
    This works for both students and leaders since they share the same division role.
    Returns None if they have no division role at all.
    """
    ids = _role_ids(member)
    for key, div in DIVISIONS.items():
        if div["role_id"] and div["role_id"] in ids:
            return key
    return None


def get_lead_division(member: discord.Member) -> str | None:
    """
    If the member has the Leader role AND a division role,
    they are the lead for that division. Returns the division key.
    Returns None if they are not a division lead.
    """
    if not is_leader(member):
        return None
    return get_member_division(member)


def can_edit_freely(member: discord.Member, division: str | None = None) -> bool:
    """
    True if the member can apply edits directly without going through a request.
      - Captains / Mentors: always, regardless of division
      - Division leads (Leader + division role): only for their own division
    """
    if is_unrestricted(member):
        return True
    lead_div = get_lead_division(member)
    if lead_div is not None:
        return division is None or lead_div == division
    return False


def can_request_edit(member: discord.Member, division: str) -> bool:
    """
    True if the member is a student in the given division.
    Students have the division role but NOT the Leader role.
    """
    if is_leader(member):
        return False  # leaders edit directly, not via requests
    return get_member_division(member) == division


async def find_division_lead(guild: discord.Guild, division: str) -> discord.Member | None:
    """
    Find a member who has BOTH the Leader role AND the given division role.
    That combination makes them the lead for that division.
    """
    leader_role  = guild.get_role(LEADER_ROLE_ID)
    division_role = guild.get_role(DIVISIONS[division]["role_id"])

    if not leader_role or not division_role:
        return None

    leader_ids   = {m.id for m in leader_role.members}
    division_ids = {m.id for m in division_role.members}
    lead_ids     = leader_ids & division_ids  # members with BOTH roles

    if not lead_ids:
        return None

    # Return the first match
    lead_id = next(iter(lead_ids))
    return guild.get_member(lead_id)
