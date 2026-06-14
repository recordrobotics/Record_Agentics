"""
Meeting time math.

A "meeting" is a scheduled start moment. Two kinds count:
  • Regular meetings — derived deterministically from MEETING_DAYS (weekday +
    time interval), recurring every week. Nothing is stored for these.
  • Manual meetings — one-off meetings a captain adds with /add_meeting. These
    are stored (as UTC timestamps) in the Meetings sheet tab via utils.store.

Achievements use these to age out: an achievement disappears once
ACHIEVEMENT_MEETING_WINDOW meetings have started after it was completed.

All meeting times are interpreted in the team's TIMEZONE and returned as
timezone-aware UTC datetimes so they compare cleanly against achievement
timestamps (which are stored in UTC).
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import MEETING_DAYS, TIMEZONE, ACHIEVEMENT_MEETING_WINDOW
from utils import store

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _tz() -> ZoneInfo:
    return ZoneInfo(TIMEZONE)


# ─── Time parsing ─────────────────────────────────────────────────────────────

def _parse_clock(token: str, default_meridiem: str | None = None) -> tuple[int, int, str | None]:
    """Parse a single clock token like '7', '7:30', '7 PM', '7:30 am'."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", token.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"Could not read a time from {token!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    mer = (m.group(3) or default_meridiem)
    mer = mer.lower() if mer else None
    if mer == "pm" and hour != 12:
        hour += 12
    elif mer == "am" and hour == 12:
        hour = 0
    return hour, minute, mer


def parse_start_time(interval: str) -> tuple[int, int]:
    """
    Pull the START time (24-hour hour, minute) out of an interval string.
    Handles '7:00 AM - 9:00 PM', '7-9 PM', '7:00 PM - 9:00 PM', '19:00'.
    If the start half omits AM/PM, it borrows it from the end half.
    """
    parts = re.split(r"[-–—]", interval)
    end_mer = None
    if len(parts) > 1:
        try:
            _, _, end_mer = _parse_clock(parts[1])
        except ValueError:
            end_mer = None
    hour, minute, _ = _parse_clock(parts[0], default_meridiem=end_mer)
    return hour, minute


# ─── Building meeting datetimes ───────────────────────────────────────────────

def _local_dt_to_utc(d, hour: int, minute: int) -> datetime:
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_tz())
    return local.astimezone(timezone.utc)


def regular_meeting_datetimes(now_utc: datetime, weeks_back: int = 3) -> list[datetime]:
    """Every regular meeting occurrence in the recent past window (UTC)."""
    tz = _tz()
    today_local = now_utc.astimezone(tz).date()
    out: list[datetime] = []
    for entry in MEETING_DAYS:
        try:
            hour, minute = parse_start_time(entry["time"])
        except ValueError:
            continue
        wd = entry["weekday"]
        for back in range(0, weeks_back * 7 + 7):
            d = today_local - timedelta(days=back)
            if d.weekday() == wd:
                out.append(_local_dt_to_utc(d, hour, minute))
    return out


def recent_past_meetings(now_utc: datetime | None = None,
                         n: int = ACHIEVEMENT_MEETING_WINDOW) -> list[datetime]:
    """The `n` most recent meeting start times that have already passed, newest first."""
    now = now_utc or datetime.now(timezone.utc)
    times = [t for t in regular_meeting_datetimes(now) if t <= now]
    for mtg in store.get_meetings():
        raw = str(mtg.get("datetime", "")).strip()
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if t <= now:
            times.append(t)
    times.sort(reverse=True)
    return times[:n]


def resolve_meeting_datetime(day: str, time_interval: str) -> datetime | None:
    """
    Turn a captain's /add_meeting input into a UTC datetime.
    `day` may be a weekday name ('Friday', 'fri') or a date
    ('2026-06-20', '6/20', '06/20/2026'). Returns None if unparseable.
    """
    try:
        hour, minute = parse_start_time(time_interval)
    except ValueError:
        return None
    tz = _tz()
    day = day.strip()

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d"):
        try:
            d = datetime.strptime(day, fmt).date()
        except ValueError:
            continue
        if fmt == "%m/%d":
            d = d.replace(year=datetime.now(tz).year)
        return _local_dt_to_utc(d, hour, minute)

    dl = day.lower()
    wd = next((i for i, name in enumerate(_WEEKDAYS) if name.startswith(dl) and len(dl) >= 3), None)
    if wd is None:
        return None
    now_local = datetime.now(tz)
    days_ahead = (wd - now_local.weekday()) % 7
    candidate = _local_dt_to_utc(now_local.date() + timedelta(days=days_ahead), hour, minute)
    if candidate <= datetime.now(timezone.utc):
        candidate = _local_dt_to_utc(now_local.date() + timedelta(days=days_ahead + 7), hour, minute)
    return candidate


def format_meeting_label(dt_utc: datetime, time_interval: str) -> str:
    """Human label like 'Friday, 6/20 · 7:00 PM - 9:00 PM' in local time."""
    local = dt_utc.astimezone(_tz())
    return f"{_WEEKDAY_NAMES[local.weekday()]}, {local.month}/{local.day} · {time_interval.strip()}"
