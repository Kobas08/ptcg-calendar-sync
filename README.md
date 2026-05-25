# Pokemon TCG Event → Google Calendar Sync

Fetches nearby Pokemon TCG events (League Cups, League Challenges, Pre Releases) from [pokedata.ovh](https://pokedata.ovh) and syncs them to a shared Google Calendar. Supports multiple locations via per-location `.env` files.

## How it works

- Events are keyed by their API `guid` — re-running is idempotent, never duplicates
- A SHA-256 hash of each event's meaningful fields detects updates between runs
- Events are categorized as **NEW**, **UPDATED**, **UNCHANGED**, or **MISSING** (previously seen, future-dated, no longer returned by the API — likely cancelled)
- Past events are pruned from the local store automatically
- Timezone is derived automatically from the provided lat/long
- Calendar titles are normalized to `<Shop> - <Event Type>` in title case
- Events are color-coded: Blueberry = Cups, Peacock = Challenges, Tangerine = Pre Releases
- Default durations: 4 hours for Cups, 3 hours for Challenges and Pre Releases

## One-time setup

### 1. Google Cloud

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com) and enable the **Google Calendar API**
2. Create a service account and download its JSON key — save it as `google_api_credentials.json` next to this script
3. Share each target Google Calendar with the service account email (`name@project.iam.gserviceaccount.com`), granting **Make changes to events** permission
4. Copy each calendar's ID from Google Calendar → Settings → Integrate calendar → **Calendar ID**

### 2. Server setup

```bash
./setup.sh
```

This creates a local `venv/`, installs dependencies, and prints the cron line to add.

Then add the printed line to your crontab:

```bash
crontab -e
```

## Adding a location

1. Copy `.env.example` to a new file, e.g. `charlotte.env`, and fill in the values:

```env
LAT=35.227
LONG=-80.843
CALENDAR_ID=your_calendar_id@group.calendar.google.com
STORE_PATH=events_store_charlotte.json
CUP_RADIUS=140
CHALLENGE_RADIUS=90
PRERELEASE_RADIUS=50
```

2. Add a line to `run.sh`:

```bash
python "$SCRIPT_DIR/fetch_events.py" --env-file "$SCRIPT_DIR/charlotte.env" "$@"
```

Each location uses its own `STORE_PATH` so their state doesn't intermingle.

## Configuration

All options can be set in a `.env` file or passed as CLI args. CLI args take priority over `.env`.

| Variable | Arg | Description |
|---|---|---|
| `LAT` | `--lat` | Latitude of search center |
| `LONG` | `--long` | Longitude of search center |
| `CALENDAR_ID` | `--calendar-id` | Google Calendar ID |
| `STORE_PATH` | `--store` | Path to local events store JSON |
| `CUP_RADIUS` | `--cup-radius` | Search radius for League Cups (miles) |
| `CHALLENGE_RADIUS` | `--challenge-radius` | Search radius for League Challenges (miles) |
| `PRERELEASE_RADIUS` | `--prerelease-radius` | Search radius for Pre Releases (miles) |
| `GOOGLE_API_CREDENTIALS` | `--credentials` | Path to service account JSON (default: `google_api_credentials.json`) |
| `LOCATION_NAME` | `--location-name` | Human-readable label shown in Discord notifications |
| `DISCORD_WEBHOOK_URL` | `--webhook-url` | Discord webhook URL — omit to skip notifications |

## Usage

```bash
# Interactive — prompts before each calendar change
python fetch_events.py --env-file raleigh.env

# Summary only, no calendar changes
python fetch_events.py --env-file raleigh.env --summary

# Dry run — full flow without touching the calendar
python fetch_events.py --env-file raleigh.env --dry-run

# Automated — no prompts, suitable for cron
python fetch_events.py --env-file raleigh.env --auto
```

## Weekly digest

`weekly_digest.py` reads upcoming events from one or more Google Calendars and posts a combined text calendar to Discord. It is intended to run once a week, independently of `run.sh`.

```bash
./weekly_digest.sh

# Preview without sending
./weekly_digest.sh --dry-run

# Look ahead two weeks
./weekly_digest.sh --days 14
```

### How it works

- Reads events from every configured calendar for the next 7 days (configurable)
- Events appearing in multiple calendars are deduplicated and shown once with all source locations listed
- Multi-day all-day events (e.g. regionals spanning a full weekend) appear on every day they cover
- Each event line prefixes the location label and links the store's Discord role or channel if configured

Example output:

```
📅 Pokemon TCG — May 25 to May 31

**Saturday, May 25**
• [RDU, Charlotte] <@&role> - Regional Championship

**Sunday, May 26**
• [RDU, Charlotte] <@&role> - Regional Championship

**Wednesday, May 29**
• [RDU] <#channel> - League Challenge @ 7:00 PM
```

### Store mentions

Create `store_mentions.json` (see `store_mentions.json.example`) to map shop names to Discord roles or channels:

```json
{
  "My Local Game Shop": "<@&123456789012345678>",
  "Online Store": "<#987654321098765432>"
}
```

Use `<@&ROLE_ID>` for a role ping, `<#CHANNEL_ID>` for a channel link. Shops without an entry fall back to displaying the shop name as plain text. The file is gitignored.

### Adding a location to the digest

Add the new `.env` file to the `--env-files` list in `weekly_digest.sh`. The webhook URL is read from the first `.env` file that defines `DISCORD_WEBHOOK_URL`.

### Weekly digest configuration

| Arg | Description |
|---|---|
| `--env-files` | One or more `.env` files to read (space-separated) |
| `--days` | Number of days to look ahead (default: 7) |
| `--store-mentions` | Path to store mentions JSON (default: `store_mentions.json`) |
| `--webhook-url` | Discord webhook URL (overrides `DISCORD_WEBHOOK_URL` in env) |
| `--dry-run` | Print the calendar without sending to Discord |

## Tips

- On first run against a new calendar every event will appear as NEW — run interactively once to seed the store, then switch to `--auto` for cron
- Start with `--dry-run` when setting up a new location to validate titles, colors, and timezones before touching the real calendar
- **MISSING** events may be cancelled, postponed, or outside the search radius after a store relocation — review before deleting
- Changing `CALENDAR_ID` and re-running migrates all events to the new calendar with no manual export needed
