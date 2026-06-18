from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from config import DIVISIONS
import calendar
import datetime
import os
import re

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Distinct footnote markers used to tie a day in the month grid to its detail
# line below. Keycap digits first, then lettered fallbacks for busy months.
FOOTNOTE_MARKERS = [
    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
    "🅰️", "🅱️", "🆎", "🅾️", "🆑", "🆒", "🆓", "🆔", "🆕", "🆖",
]


def get_upcoming_events(max_results: int = 15) -> list[dict]:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    service = build("calendar", "v3", credentials=creds)
    now = datetime.datetime.utcnow().isoformat() + "Z"
    result = service.events().list(
        calendarId=os.getenv("CALENDAR_ID"),
        timeMin=now,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def _event_start(event: dict) -> datetime.datetime | None:
    """Parsed start datetime for an event, or None if it can't be parsed."""
    raw = event["start"].get("dateTime", event["start"].get("date"))
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def format_event_time(event: dict) -> str:
    raw = event["start"].get("dateTime", event["start"].get("date"))
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if "dateTime" in event["start"]:
            return dt.strftime("%a, %b %d  •  %I:%M %p")
        else:
            return dt.strftime("%a, %b %d  •  All Day")
    except Exception:
        return raw


def parse_division_tag(title: str) -> tuple[str | None, str]:
    """
    Parses [Division] prefix from an event title.
    '[Programming] Kickoff' → ('programming', 'Kickoff')
    'General meeting'       → (None, 'General meeting')
    """
    match = re.match(r"^\[(.+?)\]\s*(.+)$", title)
    if match:
        tag = match.group(1).strip().lower()
        clean_title = match.group(2).strip()
        for key in DIVISIONS:
            if key == tag or DIVISIONS[key]["name"].lower() == tag:
                return key, clean_title
    return None, title


def _sort_key(start: datetime.datetime | None) -> datetime.datetime:
    """Normalize a (possibly naive / possibly missing) start to a UTC datetime so
    mixed timed and all-day events can be sorted without TypeErrors. None sorts last."""
    if start is None:
        return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime.timezone.utc)
    return start.astimezone(datetime.timezone.utc)


def _division_emoji(div_key: str | None) -> str:
    return f"{DIVISIONS[div_key]['emoji']} " if div_key else ""


def _legend_line(marker: str, event: dict) -> str:
    """One footnote line under the grid: marker → time · division · title · location."""
    div_key, clean_title = parse_division_tag(event.get("summary", "Untitled"))
    location = event.get("location", "")
    loc = f"  📍 {location}" if location else ""
    return f"{marker}  **{format_event_time(event)}** — {_division_emoji(div_key)}{clean_title}{loc}"


def _later_line(event: dict) -> str:
    """Detail block for an event beyond the current month (list view)."""
    div_key, clean_title = parse_division_tag(event.get("summary", "Untitled"))
    location = event.get("location", "")
    loc = f"\n   📍 {location}" if location else ""
    return f"📅 {_division_emoji(div_key)}**{clean_title}**\n   🕐 {format_event_time(event)}{loc}"


def build_calendar_text(events: list[dict]) -> str:
    if not events:
        return "*No upcoming events scheduled.*"

    today = datetime.date.today()
    year, month = today.year, today.month

    # Partition into this calendar month (→ grid) vs everything later (→ list).
    in_month: list[tuple[datetime.datetime, dict]] = []
    later: list[tuple[datetime.datetime | None, dict]] = []
    for event in events:
        start = _event_start(event)
        if start and start.year == year and start.month == month:
            in_month.append((start, event))
        else:
            later.append((start, event))

    in_month.sort(key=lambda pair: _sort_key(pair[0]))

    # Assign a distinct footnote marker per in-month event and map day → markers.
    day_markers: dict[int, list[str]] = {}
    legend_lines: list[str] = []
    for idx, (start, event) in enumerate(in_month):
        marker = FOOTNOTE_MARKERS[idx] if idx < len(FOOTNOTE_MARKERS) else "🔸"
        day_markers.setdefault(start.day, []).append(marker)
        legend_lines.append(_legend_line(marker, event))

    # ── Section 1: month grid (monospace) ─────────────────────────────────────
    grid_lines = [f"{calendar.month_name[month]} {year}".center(20), "Mo Tu We Th Fr Sa Su"]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        cells = []
        for day in week:
            if day == 0:
                cells.append("  ")
            elif day in day_markers:
                # Show the first marker inline; the legend lists every event for the day.
                cells.append(f"{day:2d}{day_markers[day][0]}")
            else:
                cells.append(f"{day:2d}")
        grid_lines.append(" ".join(cells))
    sections = ["```\n" + "\n".join(grid_lines) + "\n```"]

    # ── Section 2: this-month legend ──────────────────────────────────────────
    if legend_lines:
        sections.append("📌 **This Month**\n" + "\n".join(legend_lines))

    # ── Section 3: later (list view) ──────────────────────────────────────────
    if later:
        later.sort(key=lambda pair: _sort_key(pair[0]))
        sections.append("🔭 **Later**\n" + "\n\n".join(_later_line(e) for _, e in later))

    return "\n\n".join(sections)
