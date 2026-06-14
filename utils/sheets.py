"""
Low-level Google Sheets access.

This module is intentionally thin: it knows how to connect, read a whole tab,
and overwrite a whole tab in a single batched API call. It does NOT contain any
agenda/achievement/request business logic anymore — that lives in
`utils.store`, which keeps an in-memory copy of every tab and uses the helpers
here only to hydrate once at boot and to flush batched writes on a timer.

Every call here is synchronous and blocking (gspread is sync). Callers MUST run
these from a worker thread (e.g. `asyncio.to_thread`) so the Discord event loop
is never frozen by a network round-trip.
"""

import gspread
from google.oauth2.service_account import Credentials
import os

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ─── Connection (cached) ────────────────────────────────────────────────────────
# One authorized client + one worksheet handle per tab, reused across flushes so
# we don't re-authenticate on every sync tick. Safe because the store flushes
# serially (one worker thread at a time).

_client: gspread.Client | None = None
_spreadsheet = None
_worksheets: dict[str, "gspread.Worksheet"] = {}


def get_client() -> gspread.Client:
    global _client
    if _client is None:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def _get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = get_client().open_by_key(os.getenv("GOOGLE_SHEETS_ID"))
    return _spreadsheet


def get_worksheet(tab_name: str):
    ws = _worksheets.get(tab_name)
    if ws is None:
        ws = _get_spreadsheet().worksheet(tab_name)
        _worksheets[tab_name] = ws
    return ws


def get_or_create_worksheet(tab_name: str, cols: int = 26):
    """Like get_worksheet, but creates the tab if it doesn't exist yet — so a
    newly-introduced tab (e.g. Meetings) doesn't make writes fail forever."""
    try:
        return get_worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = _get_spreadsheet().add_worksheet(title=tab_name, rows=100, cols=max(26, cols))
        _worksheets[tab_name] = ws
        print(f"[Sheets] Created missing tab '{tab_name}'.")
        return ws


# ─── Read ───────────────────────────────────────────────────────────────────────

def read_table(tab_name: str) -> tuple[list[str], list[dict]]:
    """
    Read a whole tab. Returns (header, records) where header is the literal
    first-row column names and records is one dict per data row keyed by header.

    Returns ([], []) instead of raising if the tab is empty, header-less, or
    missing — so a fresh spreadsheet doesn't crash boot.
    """
    try:
        ws = get_worksheet(tab_name)
        rows = ws.get_all_values()
    except gspread.WorksheetNotFound:
        # A genuinely-absent tab is "empty", not an error.
        print(f"[Sheets] Tab '{tab_name}' not found. Create it in your spreadsheet.")
        return [], []
    # Any other error (network, auth, quota) is propagated so callers can tell a
    # real failure apart from an empty tab — important so a transient read error
    # never gets mistaken for "the sheet is now empty".

    if not rows:
        return [], []
    header = rows[0]
    if not any(h.strip() for h in header):
        return [], []

    records = []
    for row in rows[1:]:
        padded = row + [""] * (len(header) - len(row))
        records.append(dict(zip(header, padded)))
    return header, records


# ─── Batched write ──────────────────────────────────────────────────────────────

def overwrite_table(tab_name: str, header: list[str], rows: list[list], prev_row_count: int) -> int:
    """
    Replace the entire contents of a tab with `header` + `rows` in ONE API call.

    Instead of issuing one append/update/delete request per changed cell (the old
    behaviour, which could be dozens of calls for a single panel update), we write
    the full table as a single rectangular range. If the table shrank since the
    last write we pad with blank rows so deleted rows are cleared in the same call.

    Returns the number of data rows written, so the caller can track the row count
    for the next flush's padding.
    """
    width = len(header)
    ws = get_or_create_worksheet(tab_name, cols=width)
    values = [header] + [r + [""] * (width - len(r)) for r in rows]

    # Pad with blank rows to overwrite any rows that existed before but don't now.
    if prev_row_count > len(rows):
        blank = [""] * width
        values.extend([blank] * (prev_row_count - len(rows)))

    ws.update("A1", values, value_input_option="RAW")
    return len(rows)
