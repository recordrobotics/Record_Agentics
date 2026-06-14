"""
In-memory front-facing store with async write-behind to Google Sheets.

Why this exists
---------------
gspread is synchronous and blocking. Every read/write is a network round-trip
that, if called inline from a button click or modal submit, freezes the entire
Discord event loop — the bot stops responding to everyone until the API answers.
Adding several tasks at once made that delay stack up.

How it works
------------
1. At boot, `hydrate()` reads every tab once into memory (the only startup read).
2. All reads (`get_agenda_tasks`, `get_achievements`, `get_request`, …) are
   served instantly from these in-memory lists — zero API calls, no blocking.
3. All writes (`add_agenda_task`, `toggle_agenda_task`, `create_request`, …)
   mutate the in-memory list immediately so the UI reflects the change at once,
   then mark that tab "dirty" — they enqueue work instead of calling the API.
4. A background loop (`start_flusher`) drains the dirty set at a fixed rate
   (`SHEETS_FLUSH_SECONDS`). For each dirty tab it writes the WHOLE table in a
   single batched API call via `sheets.overwrite_table` — so ten task additions
   between two ticks cost one API call, not ten. The blocking write runs in a
   worker thread (`asyncio.to_thread`) so it never freezes the event loop.

5. `resync()` periodically pulls the sheet back into memory (push-then-read)
   so manual edits made directly in the spreadsheet — like deleting a row — show
   up in the bot. It skips the pull while a local write is pending and aborts on
   a read error, so it never clobbers an in-flight write or wipes data on a blip.

Trade-off: if you edit the sheet at the same instant the bot is writing, the bot
wins that round; your edit is picked up on the next quiet resync.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

from config import SHEETS_FLUSH_SECONDS
from utils import sheets

# Canonical columns per tab. The real sheet header is preserved on hydrate; any
# of these columns missing from it are appended so audit data is never dropped.
CANONICAL_HEADERS: dict[str, list[str]] = {
    "Agenda":       ["id", "task", "done", "division", "done_at", "editor", "approver"],
    "Achievements": ["id", "achievement", "division", "achieved_at", "editor", "approver"],
    "Requests":     ["id", "division", "action", "payload",
                     "requester_id", "requester_name", "status", "timestamp"],
    "Meetings":     ["id", "datetime", "label", "type"],
}

# ─── In-memory state ────────────────────────────────────────────────────────────
_tables: dict[str, list[dict]] = {tab: [] for tab in CANONICAL_HEADERS}
_headers: dict[str, list[str]] = {}
_row_counts: dict[str, int] = {}   # rows last written to each tab (for flush padding)
_dirty: set[str] = set()           # tabs awaiting a flush — this is the write queue
_clean: dict[str, str] = {}        # tab → signature of content last synced with the sheet

_loaded = False
_flusher_task: asyncio.Task | None = None
_stopped = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _mark_dirty(tab: str) -> None:
    _dirty.add(tab)


# ─── Hydrate / lifecycle ────────────────────────────────────────────────────────

async def _load_tables() -> tuple[dict, dict, dict, set]:
    """Read every tab from Sheets into fresh dicts. Raises if any read fails
    (so a transient error is never mistaken for an empty sheet)."""
    tables: dict[str, list[dict]] = {}
    headers: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    backfill: set[str] = set()

    for tab, canonical in CANONICAL_HEADERS.items():
        header, records = await asyncio.to_thread(sheets.read_table, tab)
        if not header:
            header = list(canonical)
        else:
            header = header + [c for c in canonical if c not in header]
        headers[tab] = header

        normalized = []
        for rec in records:
            row = dict(rec)
            if tab == "Requests":
                row["payload"] = _parse_payload(row.get("payload"))
            if tab in ("Agenda", "Achievements") and not str(row.get("id", "")).strip():
                # Rows added/edited by hand (or sheets predating the id column)
                # get a stable id so duplicate names can be told apart.
                row["id"] = _new_id()
                backfill.add(tab)
            normalized.append(row)
        tables[tab] = normalized
        counts[tab] = len(records)
    return tables, headers, counts, backfill


def _apply_tables(tables: dict, headers: dict, counts: dict, backfill: set) -> None:
    _tables.clear(); _tables.update(tables)
    _headers.clear(); _headers.update(headers)
    _row_counts.clear(); _row_counts.update(counts)
    # Record what the sheet currently holds. A tab is only ever re-written when
    # in-memory content diverges from this signature — so a redundant flush can't
    # clobber an edit made directly in the spreadsheet.
    _dirty.clear()
    for tab in CANONICAL_HEADERS:
        if tab in backfill:
            _clean[tab] = "__force_write__"  # memory has new ids the sheet lacks
            _mark_dirty(tab)
        else:
            _clean[tab] = _sig(_serialize_tab(tab))
    _migrate_legacy_done_tasks()


async def hydrate() -> None:
    """Load every tab into memory. Call once at boot before serving any panel."""
    global _loaded
    try:
        tables, headers, counts, backfill = await _load_tables()
        _apply_tables(tables, headers, counts, backfill)
        for tab in CANONICAL_HEADERS:
            print(f"[Store] Hydrated {tab}: {len(_tables[tab])} row(s).")
    except Exception as e:
        # Don't crash boot if Sheets is briefly unreachable; start empty. We do
        # NOT mark anything dirty, so the flusher won't overwrite the real sheet.
        print(f"[Store] Hydrate failed ({e}); starting with empty data.")
    _loaded = True


async def resync() -> bool:
    """Pull the latest sheet contents back into memory so manual edits made
    directly in the spreadsheet (e.g. deleting a row) show up in the bot.

    Safe ordering: push local writes first, then read. Skips the read if a
    local change is pending (so we never clobber an in-flight write) and aborts
    on any read error (so a network blip never wipes good in-memory data).
    Returns True if memory was refreshed from the sheet."""
    await flush_now()
    if _dirty:
        return False  # a write got queued during the flush — refresh later
    try:
        tables, headers, counts, backfill = await _load_tables()
    except Exception as e:
        print(f"[Store] Resync read failed ({e}); keeping current data.")
        return False
    if _dirty:
        return False  # a local write happened while reading — prefer local
    _apply_tables(tables, headers, counts, backfill)
    return True


def _migrate_legacy_done_tasks() -> None:
    """Old data model kept done tasks in Agenda until a timer moved them. Now a
    completed task moves to Achievements immediately, so sweep any leftover
    done==TRUE agenda rows into Achievements on boot."""
    legacy = [r for r in _tables["Agenda"] if str(r.get("done", "FALSE")).upper() == "TRUE"]
    for row in legacy:
        complete_task(row.get("id"),
                      editor=row.get("editor", ""), approver=row.get("approver", ""))
    if legacy:
        print(f"[Store] Migrated {len(legacy)} already-done task(s) to Achievements.")


def start_flusher() -> None:
    """Start the background write-behind loop. Call once after hydrate()."""
    global _flusher_task, _stopped
    _stopped = False
    if _flusher_task is None or _flusher_task.done():
        _flusher_task = asyncio.create_task(_flush_loop())


async def _flush_loop() -> None:
    while not _stopped:
        await asyncio.sleep(SHEETS_FLUSH_SECONDS)
        await flush_now()


async def flush_now() -> None:
    """Flush every dirty tab to Sheets, one batched API call per tab."""
    if not _dirty:
        return
    tabs = list(_dirty)
    _dirty.clear()
    for tab in tabs:
        header = _headers[tab]
        # Snapshot synchronously (no await) so the rows are internally consistent.
        rows = _serialize_tab(tab)
        sig = _sig(rows)
        # Skip if memory already matches what's on the sheet — never overwrite a
        # tab we have no real change for (this is what protects manual edits).
        if sig == _clean.get(tab):
            continue
        prev = _row_counts.get(tab, 0)
        try:
            written = await asyncio.to_thread(
                sheets.overwrite_table, tab, header, rows, prev
            )
            _row_counts[tab] = written
            _clean[tab] = sig
        except Exception as e:
            print(f"[Store] Flush of '{tab}' failed, will retry: {e}")
            _dirty.add(tab)  # re-queue for the next tick


async def stop() -> None:
    """Stop the flusher and write out any pending changes (best effort)."""
    global _stopped
    _stopped = True
    if _flusher_task:
        _flusher_task.cancel()
    await flush_now()


def _serialize_row(tab: str, row: dict, header: list[str]) -> list:
    out = []
    for col in header:
        val = row.get(col, "")
        if tab == "Requests" and col == "payload" and isinstance(val, (dict, list)):
            val = json.dumps(val)
        out.append("" if val is None else str(val))
    return out


def _serialize_tab(tab: str) -> list[list]:
    header = _headers[tab]
    return [_serialize_row(tab, row, header) for row in _tables[tab]]


def _sig(rows: list[list]) -> str:
    return json.dumps(rows, sort_keys=True)


def _parse_payload(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ─── Agenda ─────────────────────────────────────────────────────────────────────

def get_agenda_tasks(division: str | None = None) -> list[dict]:
    rows = [dict(r) for r in _tables["Agenda"]]
    if division:
        return [r for r in rows if r.get("division", "").lower() == division.lower()]
    return rows


def add_agenda_task(task: str, division: str, editor: str = "", approver: str = "") -> str:
    task_id = _new_id()
    _tables["Agenda"].append({
        "id": task_id, "task": task, "done": "FALSE", "division": division,
        "done_at": "", "editor": editor, "approver": approver,
    })
    _mark_dirty("Agenda")
    return task_id


def resolve_task_id(task_name: str, division: str) -> str | None:
    """First agenda task id matching name + division. Fallback for callers that
    only have a name (e.g. legacy requests created before tasks carried ids)."""
    for row in _tables["Agenda"]:
        if row.get("task") == task_name and row.get("division", "").lower() == division.lower():
            return row.get("id")
    return None


def delete_agenda_task(task_id: str) -> None:
    before = len(_tables["Agenda"])
    _tables["Agenda"] = [r for r in _tables["Agenda"] if r.get("id") != task_id]
    if len(_tables["Agenda"]) != before:
        _mark_dirty("Agenda")


def complete_task(task_id: str, editor: str = "", approver: str = "") -> bool:
    """Move an agenda task straight to Achievements. Returns True if it existed."""
    for row in _tables["Agenda"]:
        if row.get("id") == task_id:
            add_achievement(
                row.get("task", ""), row.get("division", "general"),
                editor=editor or row.get("editor", ""),
                approver=approver or row.get("approver", ""),
            )
            delete_agenda_task(task_id)
            return True
    return False


# ─── Achievements ────────────────────────────────────────────────────────────────

def get_achievements(division: str | None = None) -> list[dict]:
    rows = [dict(r) for r in _tables["Achievements"]]
    if division:
        return [r for r in rows if r.get("division", "").lower() == division.lower()]
    return rows


def add_achievement(text: str, division: str, editor: str = "", approver: str = "") -> str:
    ach_id = _new_id()
    _tables["Achievements"].append({
        "id": ach_id, "achievement": text, "division": division,
        "achieved_at": _now_iso(), "editor": editor, "approver": approver,
    })
    _mark_dirty("Achievements")
    return ach_id


def resolve_achievement_id(name: str, division: str) -> str | None:
    """First achievement id matching name + division (fallback for legacy requests)."""
    for row in _tables["Achievements"]:
        if row.get("achievement") == name and row.get("division", "").lower() == division.lower():
            return row.get("id")
    return None


def uncomplete_achievement(ach_id: str, editor: str = "", approver: str = "") -> bool:
    """Move an achievement back to the Agenda (the 'oops, undo' action).
    Returns True if it existed."""
    for row in _tables["Achievements"]:
        if row.get("id") == ach_id:
            add_agenda_task(
                row.get("achievement", ""), row.get("division", "general"),
                editor=editor or row.get("editor", ""),
                approver=approver or row.get("approver", ""),
            )
            _tables["Achievements"] = [r for r in _tables["Achievements"] if r.get("id") != ach_id]
            _mark_dirty("Achievements")
            return True
    return False


def prune_achievements_before(cutoff: datetime) -> int:
    """Drop achievements completed before `cutoff` (a tz-aware datetime).
    Used to age out achievements once they fall outside the meeting window.
    Returns count removed."""
    kept, removed = [], 0
    for r in _tables["Achievements"]:
        ts = str(r.get("achieved_at", "")).strip()
        if ts:
            try:
                if datetime.fromisoformat(ts) < cutoff:
                    removed += 1
                    continue
            except ValueError:
                pass
        kept.append(r)
    if removed:
        _tables["Achievements"] = kept
        _mark_dirty("Achievements")
    return removed


# ─── Requests ────────────────────────────────────────────────────────────────────

def create_request(division: str, action: str, payload: dict,
                   requester_id: int, requester_name: str) -> str:
    request_id = str(uuid.uuid4())[:8].upper()
    _tables["Requests"].append({
        "id": request_id,
        "division": division,
        "action": action,
        "payload": payload,
        "requester_id": str(requester_id),
        "requester_name": requester_name,
        "status": "pending",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
    _mark_dirty("Requests")
    return request_id


def get_request(request_id: str) -> dict | None:
    for r in _tables["Requests"]:
        if str(r.get("id", "")).upper() == request_id.upper():
            row = dict(r)
            row["payload"] = _parse_payload(row.get("payload"))
            return row
    return None


def update_request_status(request_id: str, status: str) -> None:
    for r in _tables["Requests"]:
        if str(r.get("id", "")).upper() == request_id.upper():
            r["status"] = status
            _mark_dirty("Requests")
            return


# ─── Meetings ────────────────────────────────────────────────────────────────────
# Only manually-added meetings are stored here; regular meetings are derived
# deterministically from the schedule in utils.meetings.

def add_meeting(dt_iso: str, label: str, mtype: str = "manual") -> str:
    meeting_id = _new_id()
    _tables["Meetings"].append({
        "id": meeting_id, "datetime": dt_iso, "label": label, "type": mtype,
    })
    _mark_dirty("Meetings")
    return meeting_id


def get_meetings() -> list[dict]:
    return [dict(r) for r in _tables["Meetings"]]
