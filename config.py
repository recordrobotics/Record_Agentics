import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  STAFF ROLES
#  These roles have full unrestricted access
#  to edit anything across all divisions
# ─────────────────────────────────────────────
CAPTAIN_ROLE_ID = int(os.getenv("CAPTAIN_ROLE_ID", 0))
MENTOR_ROLE_ID  = int(os.getenv("MENTOR_ROLE_ID",  0))

UNRESTRICTED_ROLE_IDS: list[int] = [
    CAPTAIN_ROLE_ID,
    MENTOR_ROLE_ID,
]

# ─────────────────────────────────────────────
#  LEADER ROLE
#  A member with BOTH this role AND a division
#  role is treated as the lead for that division.
#  Leader + Engineers role = Engineering lead
#  Leader + Programmers role = Programming lead
# ─────────────────────────────────────────────
LEADER_ROLE_ID = int(os.getenv("LEADER_ROLE_ID", 0))

# ─────────────────────────────────────────────
#  DIVISIONS
#  Each division only needs one role ID now.
#  role_id → the Discord role shared by everyone
#            in that division (students AND leaders)
#
#  To add a new division: copy a block, give it
#  a key, fill in the name/emoji/role_id, and
#  add the env var to your .env file.
# ─────────────────────────────────────────────
DIVISIONS: dict[str, dict] = {
    "engineers": {
        "name":    "Engineers",
        "emoji":   "⚙️",
        "role_id": int(os.getenv("ENGINEERS_ROLE_ID", 0)),
    },
    "programmers": {
        "name":    "Programmers",
        "emoji":   "💻",
        "role_id": int(os.getenv("PROGRAMMERS_ROLE_ID", 0)),
    },
    "marketing": {
        "name":    "Marketing",
        "emoji":   "📣",
        "role_id": int(os.getenv("MARKETING_ROLE_ID", 0)),
    },
    "cad": {
        "name":    "CAD",
        "emoji":   "📐",
        "role_id": int(os.getenv("CAD_ROLE_ID", 0)),
    },
    "drivers": {
        "name":    "Drivers",
        "emoji":   "🕹️",
        "role_id": int(os.getenv("DRIVERS_ROLE_ID", 0)),
    },
}

# ─────────────────────────────────────────────
#  AGENDA → ACHIEVEMENT AUTO-MOVE
#  How many hours after a task is checked done
#  before it automatically moves to Achievements.
#  Change this to any number you want.
#  Examples: 24 = one day, 48 = two days
# ─────────────────────────────────────────────
DONE_TO_ACHIEVEMENT_HOURS = 24   # hours a task stays crossed-off in Agenda before moving to Achievements
ACHIEVEMENT_DISPLAY_HOURS = 24   # hours an achievement stays visible before being removed entirely

# ─────────────────────────────────────────────
#  SHEETS WRITE-BEHIND SYNC
#  How often the in-memory store flushes pending
#  changes to Google Sheets, in seconds. Writes
#  are instant in-memory; this only controls how
#  fast they reach the spreadsheet. Lower = less
#  data lost on a crash; higher = fewer API calls.
# ─────────────────────────────────────────────
SHEETS_FLUSH_SECONDS = 8

# ─────────────────────────────────────────────
#  CHANNEL IDs
#  Agenda and Achievements share one channel now
# ─────────────────────────────────────────────
CALENDAR_CHANNEL_ID        = int(os.getenv("CALENDAR_CHANNEL_ID",        0))
AGENDA_ACHIEVEMENTS_CHANNEL_ID = int(os.getenv("AGENDA_ACHIEVEMENTS_CHANNEL_ID", 0))
SIGNUPS_CHANNEL_ID         = int(os.getenv("SIGNUPS_CHANNEL_ID",         0))

# ─────────────────────────────────────────────
#  SIGNUP SCHEDULE
# ─────────────────────────────────────────────
SIGNUP_WEEKDAY = 0    # 0 = Monday … 6 = Sunday  (day the poll is auto-posted)
SIGNUP_HOUR    = 9    # 24-hour format

# Each entry is one poll option. weekday: 0=Mon … 6=Sun. time: display string.
# The bot calculates the actual date of the next occurrence from the post date.
MEETING_DAYS: list[dict] = [
    {"weekday": 0, "time": "7:00 AM - 9:00 PM"},  # Monday
	{"weekday": 3, "time": "7:00 PM - 9:00 PM"},  # Thursday
]

POLL_DURATION_HOURS = 168   # how long the poll stays open

# ─────────────────────────────────────────────
#  REQUEST NOTIFICATIONS
# ─────────────────────────────────────────────
RESULT_DM_DISPLAY_HOURS = 24  # hours before approved/denied DM is auto-deleted from requester's DM
