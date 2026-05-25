from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from config import DIVISIONS
import datetime
import os
import re

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


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


def build_calendar_text(events: list[dict]) -> str:
    if not events:
        return "*No upcoming events scheduled.*"

    # Group by division (None = general / team-wide)
    groups: dict[str | None, list[str]] = {}
    for event in events:
        title = event.get("summary", "Untitled")
        div_key, clean_title = parse_division_tag(title)
        time_str = format_event_time(event)
        location = event.get("location", "")
        loc = f"\n   📍 {location}" if location else ""
        line = f"📅 **{clean_title}**\n   🕐 {time_str}{loc}"
        groups.setdefault(div_key, []).append(line)

    sections: list[str] = []

    # General events first
    if None in groups:
        sections.append("**── Team-Wide ──**\n" + "\n\n".join(groups[None]))

    # Then each division
    for key, div in DIVISIONS.items():
        if key in groups:
            header = f"**── {div['emoji']} {div['name']} ──**"
            sections.append(header + "\n" + "\n\n".join(groups[key]))

    return "\n\n".join(sections)
