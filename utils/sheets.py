import gspread
from google.oauth2.service_account import Credentials
import os
import uuid
import json
from datetime import datetime, timezone

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ─── Connection ───────────────────────────────────────────────────────────────

def get_client():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

def get_worksheet(tab_name: str):
    client = get_client()
    sheet = client.open_by_key(os.getenv("GOOGLE_SHEETS_ID"))
    return sheet.worksheet(tab_name)

def safe_get_records(tab_name: str) -> list[dict]:
    """
    Safely read all records from a tab. Returns [] instead of crashing if
    the sheet is empty, has no headers, or the tab doesn't exist yet.
    """
    try:
        ws = get_worksheet(tab_name)
        rows = ws.get_all_values()
        if len(rows) < 1:
            # Sheet is completely empty — no headers
            return []
        headers = rows[0]
        if not any(h.strip() for h in headers):
            # First row exists but is all blank
            return []
        records = []
        for row in rows[1:]:
            # Pad short rows so zip doesn't drop columns
            padded = row + [""] * (len(headers) - len(row))
            records.append(dict(zip(headers, padded)))
        return records
    except gspread.WorksheetNotFound:
        print(f"[Sheets] Tab '{tab_name}' not found. Create it in your spreadsheet.")
        return []
    except Exception as e:
        print(f"[Sheets] Error reading tab '{tab_name}': {e}")
        return []


# ─── Agenda ───────────────────────────────────────────────────────────────────
# Tab: Agenda
# Headers row 1: task | done | division | done_at

def get_agenda_tasks(division: str | None = None) -> list[dict]:
    records = safe_get_records("Agenda")
    if division:
        return [r for r in records if r.get("division", "").lower() == division.lower()]
    return records

def add_agenda_task(task: str, division: str, editor: str = "", approver: str = ""):
    ws = get_worksheet("Agenda")
    ws.append_row([task, "FALSE", division, "", editor, approver])

def toggle_agenda_task(task_name: str, division: str, done: bool,
                       editor: str = "", approver: str = ""):
    """Find a task by name + division and update its done state, timestamp, and audit columns."""
    ws = get_worksheet("Agenda")
    rows = ws.get_all_values()
    if len(rows) < 1:
        return

    headers = rows[0]
    try:
        task_col = headers.index("task") + 1
        done_col = headers.index("done") + 1
    except ValueError as e:
        print(f"[Sheets] Missing expected column in Agenda tab: {e}")
        return

    def _col(name):
        try:
            return headers.index(name) + 1
        except ValueError:
            return None

    done_at_col  = _col("done_at")
    editor_col   = _col("editor")
    approver_col = _col("approver")
    done_at_value = datetime.now(timezone.utc).isoformat() if done else ""

    for i, row in enumerate(rows[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        if padded[task_col - 1] == task_name and padded[2].lower() == division.lower():
            ws.update_cell(i, done_col, "TRUE" if done else "FALSE")
            if done_at_col:
                ws.update_cell(i, done_at_col, done_at_value)
            if editor_col and editor:
                ws.update_cell(i, editor_col, editor)
            if approver_col:
                ws.update_cell(i, approver_col, approver)
            return

def get_tasks_ready_to_move(hours: int) -> list[dict]:
    """Tasks that are done AND have been done for longer than `hours`."""
    records = safe_get_records("Agenda")
    ready = []
    now = datetime.now(timezone.utc)
    for r in records:
        if str(r.get("done", "FALSE")).upper() != "TRUE":
            continue
        done_at_str = str(r.get("done_at", "")).strip()
        if not done_at_str:
            continue
        try:
            done_at = datetime.fromisoformat(done_at_str)
            if (now - done_at).total_seconds() / 3600 >= hours:
                ready.append(r)
        except ValueError:
            continue
    return ready

def delete_agenda_task(task_name: str, division: str):
    ws = get_worksheet("Agenda")
    rows = ws.get_all_values()
    if len(rows) < 1:
        return
    headers = rows[0]
    try:
        task_col = headers.index("task")
        div_col  = headers.index("division")
    except ValueError:
        return
    for i, row in enumerate(rows[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        if padded[task_col] == task_name and padded[div_col].lower() == division.lower():
            ws.delete_rows(i)
            return

def move_done_tasks_to_achievements(hours: int) -> int:
    """Move tasks that have been done for >= hours to Achievements. Returns count moved."""
    ready = get_tasks_ready_to_move(hours)
    for task in ready:
        add_achievement(
            task.get("task", ""),
            task.get("division", "general"),
            editor=task.get("editor", ""),
            approver=task.get("approver", ""),
        )
        delete_agenda_task(task.get("task", ""), task.get("division", "general"))
    return len(ready)


# ─── Achievements ─────────────────────────────────────────────────────────────
# Tab: Achievements
# Headers row 1: achievement | division

def get_achievements(division: str | None = None) -> list[dict]:
    records = safe_get_records("Achievements")
    if division:
        return [r for r in records if r.get("division", "").lower() == division.lower()]
    return records

def add_achievement(text: str, division: str, editor: str = "", approver: str = ""):
    ws = get_worksheet("Achievements")
    ws.append_row([text, division, datetime.now(timezone.utc).isoformat(), editor, approver])


def expire_old_achievements(hours: int) -> int:
    """Delete achievements whose achieved_at is older than `hours`. Returns count deleted."""
    ws = get_worksheet("Achievements")
    rows = ws.get_all_values()
    if len(rows) < 2:
        return 0
    headers = rows[0]
    try:
        achieved_at_col = headers.index("achieved_at")
    except ValueError:
        return 0  # column not present yet — nothing to expire
    now = datetime.now(timezone.utc)
    to_delete = []
    for i, row in enumerate(rows[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        ts = padded[achieved_at_col].strip()
        if not ts:
            continue
        try:
            if (now - datetime.fromisoformat(ts)).total_seconds() / 3600 >= hours:
                to_delete.append(i)
        except ValueError:
            continue
    for i in reversed(to_delete):
        ws.delete_rows(i)
    return len(to_delete)


# ─── Requests ─────────────────────────────────────────────────────────────────
# Tab: Requests
# Headers row 1: id | division | action | payload | requester_id | requester_name | status | timestamp

def create_request(
    division: str,
    action: str,
    payload: dict,
    requester_id: int,
    requester_name: str,
) -> str:
    ws = get_worksheet("Requests")
    request_id = str(uuid.uuid4())[:8].upper()
    ws.append_row([
        request_id,
        division,
        action,
        json.dumps(payload),
        str(requester_id),
        requester_name,
        "pending",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    ])
    return request_id

def get_request(request_id: str) -> dict | None:
    records = safe_get_records("Requests")
    for r in records:
        if str(r.get("id", "")).upper() == request_id.upper():
            try:
                r["payload"] = json.loads(r["payload"]) if r.get("payload") else {}
            except Exception:
                r["payload"] = {}
            return r
    return None

def update_request_status(request_id: str, status: str):
    ws = get_worksheet("Requests")
    rows = ws.get_all_values()
    if len(rows) < 1:
        return
    headers = rows[0]
    try:
        id_col     = headers.index("id")     + 1
        status_col = headers.index("status") + 1
    except ValueError:
        return
    for i, row in enumerate(rows[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        if padded[id_col - 1].upper() == request_id.upper():
            ws.update_cell(i, status_col, status)
            return
