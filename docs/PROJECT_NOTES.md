# SAMI, Seven Hills — Who's In / Who's Out Dashboard

**Repo:** `lkur008/site-signin`
**Branch:** `claude/door-access-dashboard-n2damd`
**Source system:** Inner Range Inception access-control controller (local network, firmware 6.3.0.8094)

## 1. Goal

A live "who's in / who's out" dashboard for SAMI's Seven Hills site, built entirely from the door events Inception already logs — no new hardware, no changes to the access-control configuration, just software reading what's already recorded.

## 2. Architecture

| File | Lines | Role |
|---|---|---|
| `door-dashboard.html` | ~756 | The entire frontend. Single static HTML file, vanilla JS, inline CSS, no build step, no framework, no dependencies beyond two Google Fonts. |
| `proxy.py` | ~187 | A Python 3 standard-library-only script. Runs on a machine on the same LAN as the Inception controller (a Raspberry Pi). Serves the HTML file *and* proxies/authenticates API calls to Inception. |
| `door-dashboard.service` | — | systemd unit template so the proxy survives reboots on the Pi. |
| `.gitignore` | — | Keeps the saved-credentials file and `__pycache__` out of git. |

### Why a proxy exists at all

Inception's REST API sends **no CORS headers**. A browser page that isn't served by the controller itself cannot call the API directly — every `fetch()` was silently blocked with "Failed to fetch," confirmed by comparing to a working direct browser login (which succeeds, proving the network path is fine — it's specifically a CORS block). The proxy sits between the browser and Inception:

```
Browser  --(same-origin, no CORS)-->  proxy.py (on the Pi)  --(server-to-server, CORS doesn't apply)-->  Inception controller
```

### Authentication

Per Inception's own `/ApiDoc` page, authenticated requests need either an API Token or a session login. The proxy uses session login:

1. On startup, `POST /api/v1/authentication/login` with `{"Username": ..., "Password": ...}`.
2. The response's `UserID` field (despite the name) holds the session ID — an all-zero GUID means login failed.
3. Every proxied request gets `Cookie: LoginSessId=<session-id>` attached by the proxy.
4. On a `401` from Inception, the proxy re-logs-in once automatically and retries (sessions expire after 10 minutes idle).

Credentials are prompted for in the terminal (not passed as a CLI argument, so they never appear in shell history or `ps`), and can optionally be saved to `~/.door-dashboard-config.json` (`chmod 600`, deliberately outside the git repo) so a systemd service can start unattended on boot with no terminal available to prompt into.

## 3. The investigation process

A significant part of this project was *discovering* what Inception's API actually exposes — official docs exist, but the real behavior had to be verified against the live system at each step.

### 3.1 First attempt: per-user location field (dead end, but ruled out properly)

Inception's `GET /api/v1/control/user/{id}/state` is documented as returning `Info1` = "the user's current location." Built the dashboard around polling this per person, expecting it to directly report presence/area — far simpler than reconstructing state from raw events.

**Result when tested against the real system:** `Info1` came back as an empty string `""` for *every* user, not just system/service accounts. Area occupancy tracking simply isn't configured on this Inception install, so there is nothing in that field to read, for anyone. Confirmed, not guessed — this was verified via the dashboard's own "Test Connection" raw-response viewer before abandoning the approach.

### 3.2 Pivot: the raw access-event log

Switched to `GET /api/v1/review?categoryFilter=Access`, Inception's actual audit trail of door swipes. Documented response shape, confirmed field-by-field against real data pulled from the live system:

```jsonc
// ReviewQueryResult
{ "Offset": 0, "Count": N, "Data": [ /* ApiReviewObject[] */ ] }

// ApiReviewObject
{
  "ID": "guid",
  "Description": "Door Access Granted for User",   // event type, human text
  "When": "2026-08-12T09:47:51.7151538+10:00",      // ISO 8601 with real UTC offset
  "ReferenceTime": "639220888717151538",            // opaque cursor for incremental polling
  "Who": "Spare L1 -3",  "WhoID": "guid",            // the person
  "What": "L1 Main Entry Maglock",  "WhatID": "guid",// the door
  "Where": "L1 Main Entry - Entry", "WhereID": "guid"// the specific reader/direction
}
```

Query parameters used: `categoryFilter=Access`, `dir=asc|desc`, `start`/`end` (ISO timestamps) for the initial day's sync, `referenceId`/`referenceTime` for cheap incremental polling (only fetch events newer than the last one already processed), `limit`/`offset` for pagination (max 1000/request).

### 3.3 Bug found: every swipe was being counted twice

A real raw dump pulled via the dashboard's Test Connection button revealed Inception logs **four separate review rows per physical swipe**:

```
Door Unlocked               (Who: "")
Door Access Granted for User (Who: "Spare L1 -3", Where: "L1 Main Entry - Entry")
User Location Updated        (Who: "Spare L1 -3")   <-- also carries the real person!
Door Locked                  (Who: "")
```

The original filter only checked for a non-empty `Who`, so both `Door Access Granted for User` *and* `User Location Updated` counted as separate toggle events on the main (toggle-mode) door. Every single swipe therefore double-flipped presence and landed on the **opposite** of the real direction — a person's very first swipe of the day rendered as "Out" instead of "In."

**Fix:** require `Description` to match a configurable "grant words" list (default `"granted"`) before an event counts at all, dropping the three companion rows. Verified by replaying the exact real event dump as a unit test.

### 3.4 Bonus finding from that same dump

The real grant event's `Where` field tags the specific reader/direction used: `"L1 Main Entry - Entry"` vs `"L1 Main Entry - Exit"`. This is Inception's own signal for which side of a toggle-mode door was used — more reliable than guessing. Switched the toggle-door logic to:

1. Check `Description` against configurable in/out word overrides first.
2. Else check the `Where` suffix (`- Entry` / `- Exit`).
3. Only fall back to blind alternating parity if neither resolves.

### 3.5 Bug found: history days displayed out of order

`dayKeyOf()` built unpadded keys like `"2026-8-9"` and sorted them as plain strings. String sort is lexicographic, not numeric: `"2026-8-11"` sorts *before* `"2026-8-9"` because `'1' < '9'` at the first differing character. Zero-padding month and day (`"2026-08-09"`, `"2026-08-11"`) makes the lexicographic sort match chronological order. Verified with a unit test asserting the sorted key order matches the true chronological order.

### 3.6 Known limitation, not a bug: tailgating

Two staff members ("Chris" and "Spare L1 -2") showed "Out" while actually physically present. Tracing their raw logs showed `Entry → Exit → Exit` — the door reader's own last logged swipe for them really was tagged Exit, with nothing after it. The most plausible explanation: they walked back in behind someone else without personally badging (tailgating), which by definition leaves no event in the log to reconstruct from. No algorithm can recover data that was never captured.

**Resolution:** rather than attempting a fragile heuristic fix, added a manual **Mark In / Mark Out** control in the per-person detail view. Setting it patches presence/location/session state directly (banking any in-progress session time if marking someone Out) and flags the record `manualOverride: true` so the card shows a small "corrected" note. The override holds until that person's *next real badge event* naturally replaces the whole record (since each processed event fully rebuilds the person's state object), so it self-clears once fresh data arrives.

## 4. Current feature set

- **Dashboard tab** — everyone currently "in" today, grouped by location (Level 1 / Lab / Entry-Lobby), each card showing:
  - the door they entered through at the start of the day (`firstDoor`/`firstDoorTime`, tracked once per person per day)
  - today's running total time in the building (live-ticking while in, finalized on exit)
  - the most recently used door + time
  - current location
  - a **Print** button with dedicated print-only CSS (hides all chrome, prints a clean grouped name list)
- **Details tab** — full roster, searchable, filterable by In/Out, same per-card detail as above.
- **Click-through person history** — clicking any card opens a detail overlay with a **Last 7 days / Last 30 days** toggle. Fetches that person's raw grant events for the range, groups by local calendar day, and replays each day through a day-scoped version of the same direction/session logic (`summarizeDayForPerson`) to show: arrival door+time, last door+time, that day's total duration, and every individual tap as a small chip (color-coded in/out).
- **Manual In/Out correction** — per the tailgating case above, with a visible "corrected" indicator.
- **Daily reset** at local midnight, computed with a proper timezone offset (not UTC-naive), so multi-day-stale entries don't linger as "In" forever. Verified to round-trip correctly across timezones (tested Sydney and New York).
- **Fully configurable via Settings**, no code changes needed:
  - Proxy base URL, poll interval
  - Per-door mapping: exact API door name → friendly display name → role (**Toggle** vs **Entry only**) → associated location label
  - Word lists: which `Description` text means a real grant (vs. the companion noise rows), which means denied/ignored, optional IN/OUT overrides
  - Manual/test JSON paste-in for trying the dashboard without a live connection
- **Light theme**, page titled "SAMI, Seven Hills." (Originally built dark-first, switched to light on request; caught and fixed two contrast bugs the switch surfaced — an inline `<code>` hint with no explicit text color, and a toast notification whose text color was hardcoded to the same value as the background.)

### Outstanding

- **SAMI logo**: not yet embedded. Fetching `sami.com.au` directly is blocked by this session's outbound network egress policy, and images attached in chat don't land on this container's disk as files. Waiting on the logo being pushed into the repo (or hosted somewhere this session's network policy allows) so it can replace the amber icon placeholder.

## 5. Door configuration (defaults shipped in the dashboard)

| Raw API door name | Display name | Role | Location |
|---|---|---|---|
| `L1 Main Entry Maglock` | Entry Door | Toggle (in/out) | *(blank → shown as "Entry")* |
| `L1 Rear Entry Strike` | L1 Lift Side Entry | Entry only | Level 1 |
| `L1 Front Entry Strike` | L1 Carpark Side Entry | Entry only | Level 1 |
| `GF Front Entry Strike` | Lab Carpark Side Entry | Entry only | Lab |
| `GF Rear Entry Strike` | Lab Lift Side Entry | Entry only | Lab |

Only the main Entry Door can mark someone "Out" (it's the only door with a logged exit reader). The other four are physically entry-only — exit through them is a manual, unlogged button — so badging at one always marks someone "In" at that location, regardless of their prior state; it just can't be used to confirm they've left.

## 6. Deployment

On a Raspberry Pi (or any machine) on the same LAN as the Inception controller:

```bash
git clone https://github.com/lkur008/site-signin.git
cd site-signin
git checkout claude/door-access-dashboard-n2damd

# First run — interactive, prompts for Inception username/password,
# offers to save them to ~/.door-dashboard-config.json for unattended restarts.
python3 proxy.py http://<inception-controller-ip>

# Install as a systemd service (edit User=/paths first if not running as "pi"
# or not cloned to /home/pi/site-signin):
sudo cp door-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now door-dashboard
```

From any device on the same network: `http://<pi-ip-or-pi.local>:8787/door-dashboard.html`. In Settings, set **Proxy base URL** to that same address (the proxy, not the Inception controller directly).

Logs: `journalctl -u door-dashboard -f`

## 7. Full commit history (this branch)

```
9e87118  Fix scrambled history-day ordering; add manual In/Out correction
e999760  Rename to SAMI, Seven Hills; show first+last door; add per-person history
b38109e  Show each person's total time in the building today
fdafc19  Fix In/Out getting flipped: every swipe was processed twice
86a6247  Switch default theme from dark to light
29f1de3  Add location tracking, daily reset, print view, and your real doors
242c6eb  Switch dashboard back to the raw event log (Info1 location is unset)
ce5383a  Support unattended startup for running the proxy on a Raspberry Pi
2c8d249  Switch proxy auth from API token to username/password session login
30d9d19  Add local CORS proxy for the Inception API
d8799d7  Wire dashboard to the real Inception REST API
cdf622c  Add standalone door-access in/out dashboard
```

Each commit message carries the detailed rationale for that change; `git log -p` on any of them shows the exact diff.

## 8. Testing approach used throughout

No test framework or CI was set up for this (single-file static app, short-lived rapid iteration), but every significant logic change was verified before being shipped:

- **Unit tests** for the core state-machine functions (`processEvents`, `summarizeDayForPerson`, `whereDirection`, duration formatting, timezone-safe day-boundary calculation) — run standalone with plain Node.js by extracting the inline `<script>` block.
- **Integration tests** against a real `proxy.py` process plus a mock HTTP server standing in for the Inception controller, exercising the actual login flow, session-cookie forwarding, query-string encoding (including the `+HH:MM` timezone offset), and pagination/cursor logic end-to-end.
- **Visual verification** via headless Chromium (Playwright, pre-installed in this environment) for CSS/contrast changes (the light-theme switch) and for the click-through history UI flow, including driving real button clicks and screenshotting the result.
- Every reported real-world bug (double-counting, scrambled history, tailgating false-positive) was first **reproduced from the user's actual raw data** pulled from the live Inception system before a fix was written, and the fix was verified by replaying that same real data through a test.
