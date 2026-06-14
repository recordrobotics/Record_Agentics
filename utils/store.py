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

Trade-off: between flushes the bot is the source of truth, and manual edits made
directly in the spreadsheet are not re-read after boot (a flush would overwrite
them). The bot writes, humans read.
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
    "Achievements": ["achievement", "division", "achieved_at", "editor", "approver"],
    "Requests":     ["id", "division", "action", "payload",
                     "requester_id", "requester_name", "status", "timestamp"],
}

# ─── In-memory state ────────────────────────────────────────────────────────────
_tables: dict[str, list[dict]] = {tab: [] for tab in CANONICAL_HEADERS}
_headers: dict[str, list[str]] = {}
_row_counts: dict[str, int] = {}   # rows last written to each tab (for flush padding)
_dirty: set[str] = set()           # tabs awaiting a flush — this is the write queue

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

async def hydrate() -> None:
    """Load every tab into memory. Call once at boot before serving any panel."""
    global _loaded
    for tab, canonical in CANONICAL_HEADERS.items():
        header, records = await asyncio.to_thread(sheets.read_table, tab)
        if not header:
            header = list(canonical)
        else:
            header = header + [c for c in canonical if c not in header]
        _headers[tab] = header

        normalized = []
        backfilled = 0
        for rec in records:
            row = dict(rec)
            if tab == "Requests":
                row["payload"] = _parse_payload(row.get("payload"))
            if tab == "Agenda" and not str(row.get("id", "")).strip():
                # Old rows (or sheets created before the id column) get a stable
                # id now so duplicate task names can be told apart.
                row["id"] = _new_id()
                backfilled += 1
            normalized.append(row)
        _tables[tab] = normalized
        _row_counts[tab] = len(records)
        if backfilled:
            _mark_dirty(tab)  # persist the new ids on the next flush
        print(f"[Store] Hydrated {tab}: {len(records)} row(s).")
    _loaded = True


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
        rows = [_serialize_row(tab, row, header) for row in _tables[tab]]
        prev = _row_counts.get(tab, 0)
        try:
            written = await asyncio.to_thread(
                sheets.overwrite_table, tab, header, rows, prev
            )
            _row_counts[tab] = written
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


def toggle_agenda_task(task_id: str, done: bool,
                       editor: str = "", approver: str = "") -> None:
    done_at = _now_iso() if done else ""
    for row in _tables["Agenda"]:
        if row.get("id") == task_id:
            row["done"] = "TRUE" if done else "FALSE"
            row["done_at"] = done_at
            if editor:
                row["editor"] = editor
            row["approver"] = approver
            _mark_dirty("Agenda")
            return


def delete_agenda_task(task_id: str) -> None:
    before = len(_tables["Agenda"])
    _tables["Agenda"] = [r for r in _tables["Agenda"] if r.get("id") != task_id]
    if len(_tables["Agenda"]) != before:
        _mark_dirty("Agenda")


def get_tasks_ready_to_move(hours: int) -> list[dict]:
    """Done tasks that have been done for at least `hours`."""
    ready = []
    now = datetime.now(timezone.utc)
    for r in _tables["Agenda"]:
        if str(r.get("done", "FALSE")).upper() != "TRUE":
            continue
        done_at_str = str(r.get("done_at", "")).strip()
        if not done_at_str:
            continue
        try:
            if (now - datetime.fromisoformat(done_at_str)).total_seconds() / 3600 >= hours:
                ready.append(r)
        except ValueError:
            continue
    return ready


def move_done_tasks_to_achievements(hours: int) -> int:
    """Move long-done agenda tasks into Achievements. Returns count moved."""
    ready = get_tasks_ready_to_move(hours)
    for task in ready:
        add_achievement(
            task.get("task", ""),
            task.get("division", "general"),
            editor=task.get("editor", ""),
            approver=task.get("approver", ""),
        )
        delete_agenda_task(task.get("id"))
    return len(ready)


# ─── Achievements ────────────────────────────────────────────────────────────────

def get_achievements(division: str | None = None) -> list[dict]:
    rows = [dict(r) for r in _tables["Achievements"]]
    if division:
        return [r for r in rows if r.get("division", "").lower() == division.lower()]
    return rows


def add_achievement(text: str, division: str, editor: str = "", approver: str = "") -> None:
    _tables["Achievements"].append({
        "achievement": text, "division": division,
        "achieved_at": _now_iso(), "editor": editor, "approver": approver,
    })
    _mark_dirty("Achievements")


def expire_old_achievements(hours: int) -> int:
    """Drop achievements older than `hours`. Returns count removed."""
    now = datetime.now(timezone.utc)
    kept, removed = [], 0
    for r in _tables["Achievements"]:
        ts = str(r.get("achieved_at", "")).strip()
        if ts:
            try:
                if (now - datetime.fromisoformat(ts)).total_seconds() / 3600 >= hours:
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
