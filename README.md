# Robotics Team Discord Bot

Persistent Discord panels for Calendar, Agenda, and Achievements — backed by Google Sheets and Google Calendar. Includes a division-based permission system with an approval flow for student edit requests.

---

## Permission Tiers

| Role | Can do |
|---|---|
| Captain / Mentor / Lead Mentor | Edit anything, everywhere, no limits |
| Division Lead | Edit their own division's content directly |
| Division Student | Submit a request → division lead gets a DM to approve or deny |

---

## Features

- **Calendar** — Always-on embed, refreshes every hour from Google Calendar. Tag events with `[Division]` to group them by division
- **Agenda** — Checkbox task list grouped by division. Leads edit directly; students submit requests
- **Achievements** — Running list of wins grouped by division. Same permission flow as agenda
- **Signups** — Native Discord poll posted automatically every week. No backend needed

---

## Google Sheets Structure

Create one spreadsheet and add these tabs with exact column headers:

**Tab: Agenda**
```
task | done | division
```

**Tab: Achievements**
```
achievement | division
```

**Tab: Requests**
```
id | division | action | payload | requester_id | requester_name | status | timestamp
```

No Signups tab needed — polls are handled entirely by Discord.

---

## Google Calendar — Division Tagging

To tag a calendar event to a division, just put the division name in brackets at the start of the event title:

```
[Programming] Kickoff meeting
[Engineering] Build day
[Marketing] Sponsor outreach call
General all-hands meeting
```

The bot groups them by division in the calendar panel automatically. Events without a tag show under Team-Wide.

---

## Adding a New Division

1. Open `config.py` and copy one of the existing division blocks:
```python
"outreach": {
    "name":           "Outreach",
    "emoji":          "🤝",
    "lead_role_id":   int(os.getenv("OUTREACH_LEAD_ROLE_ID",   0)),
    "member_role_id": int(os.getenv("OUTREACH_MEMBER_ROLE_ID", 0)),
},
```
2. Add the two new role ID variables to your `.env`:
```
OUTREACH_LEAD_ROLE_ID=
OUTREACH_MEMBER_ROLE_ID=
```
3. Restart the bot. The new division shows up everywhere automatically.

---

## Setup Guide

### Step 1 — Discord Bot

1. Go to https://discord.com/developers/applications → New Application
2. Go to Bot tab → Add Bot
3. Under Privileged Gateway Intents, enable Server Members Intent and Message Content Intent
4. Copy the Token → paste into `.env` as `DISCORD_TOKEN`
5. Go to OAuth2 > URL Generator. Scopes: `bot`, `applications.commands`. Permissions: `Send Messages`, `Read Messages/View Channels`, `Manage Messages`, `Embed Links`, `Mention Everyone`
6. Open the URL to invite the bot to your server

### Step 2 — Get Role IDs

1. In Discord: User Settings > Advanced > enable Developer Mode
2. Right-click any role in Server Settings > Roles → Copy Role ID
3. Fill in all role IDs in your `.env`

### Step 3 — Get Channel IDs

Right-click each channel → Copy Channel ID → fill into `.env`

### Step 4 — Google Service Account

1. Go to https://console.cloud.google.com → New Project
2. Enable Google Sheets API and Google Calendar API
3. IAM & Admin > Service Accounts > Create Service Account
4. Open the service account > Keys tab > Add Key > JSON → download it
5. Rename the file to `credentials.json` and put it in the bot folder
6. Copy the service account email (looks like `name@project.iam.gserviceaccount.com`)

### Step 5 — Google Sheets

1. Create a spreadsheet at https://sheets.google.com
2. Share it with the service account email (give Editor access)
3. Copy the ID from the URL: `docs.google.com/spreadsheets/d/THIS_ID/edit`
4. Paste into `.env` as `GOOGLE_SHEETS_ID`
5. Create the four tabs listed above with correct headers

### Step 6 — Google Calendar

1. Open your team calendar settings at calendar.google.com
2. Share it with the service account email (Read access is fine)
3. Copy the Calendar ID from settings
4. Paste into `.env` as `CALENDAR_ID`

### Step 7 — Run the Bot

```bash
pip install -r requirements.txt
python main.py
```

### Step 8 — Initialize Panels

Run these slash commands in Discord (you need Manage Messages permission):

```
/setup_agenda
/setup_achievements
/refresh_calendar
/post_signup
```

After that everything is automatic.

---

## Hosting on Railway

1. Push to a private GitHub repo (do NOT commit `.env` or `credentials.json`)
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Add all `.env` values under Variables
4. For `credentials.json`: paste the entire JSON content as a Railway variable called `GOOGLE_CREDENTIALS_JSON`, then add this to `main.py` before `load_dotenv()`:

```python
import json, os
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if creds_json:
    with open("credentials.json", "w") as f:
        f.write(creds_json)
```

---

## Slash Commands

| Command | Who | What |
|---|---|---|
| `/setup_agenda` | Captains+ | Post or re-post the agenda panel |
| `/setup_achievements` | Captains+ | Post or re-post the achievements panel |
| `/refresh_calendar` | Captains+ | Force-refresh the calendar embed |
| `/post_signup` | Captains+ | Manually post this week's poll |
