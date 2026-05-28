"""
Pokemon TCG Event → Google Calendar Sync
=========================================

Fetches nearby Pokemon TCG events (League Cups, League Challenges, and
Pre Releases) from pokedata.ovh and syncs them to a shared Google Calendar.
Maintains a local JSON store so updated events are detected and reflected on
the calendar instead of producing duplicates.

Behavior
--------
- Each event is keyed by its API `guid`, which is also used to derive a
  deterministic Google Calendar event ID. Re-running is idempotent: same
  events get updated in place, never duplicated.
- A SHA-256 hash of the meaningful fields (name, date, time, cost, address,
  status, shop) is stored locally to detect updates between runs.
- Events are categorized as NEW, UPDATED, UNCHANGED, or MISSING (previously
  seen, future-dated, no longer in the API — likely cancelled).
- Past events are pruned from the local store automatically.
- Calendar event titles are normalized to "<Shop> - <Event Type>" in title
  case. The store's original event name is preserved in the description.
- Events are color-coded:
    * Tangerine — Pre Releases
    * Peacock   — League Challenges
    * Blueberry — League Cups
- Durations: 4 hours for Cups, 3 hours for Challenges and Pre Releases.
- All times are written to the calendar in America/New_York.

Files
-----
- events_store.json     Local state. Created automatically. Safe to commit
                        if running via CI; safe to delete to reset (but
                        every future event will then re-appear as NEW).
- service-account.json  GCP service account credentials. Required.
                        Do NOT commit this file.

One-time setup
--------------
1. Install dependencies:
       pip install requests google-api-python-client google-auth

2. Create a Google Cloud project and enable the Google Calendar API:
       https://console.cloud.google.com/

3. Create a service account in that project and download its JSON key.
   Save it next to this script as `google_api_credentials.json`.

4. Share the target Google Calendar with the service account's email
   address (looks like `name@project.iam.gserviceaccount.com`), granting
   "Make changes to events" permission.

5. Copy the calendar ID from Google Calendar → Settings → Integrate
   calendar → "Calendar ID". Set it as CALENDAR_ID in your .env file.

6. Set LAT, LONG, and search radii in your .env file (see .env.example).

Usage
-----
Interactive run (default — prompts before each calendar change):
    python script.py

Quick summary of what's new/updated/missing, no calendar changes:
    python script.py --summary

Test run that exercises the full flow without touching the calendar:
    python script.py --dry-run

Automated run (no prompts; suitable for cron / GitHub Actions):
    python script.py --auto

Recommended workflow
--------------------
- Run weekly. Stores typically post a new League Challenge each month and
  a Cup each quarter, plus prereleases on set release weeks.
- On the first run against a new calendar, every event will appear as NEW.
  Step through them once to seed the local store.
- For a fresh setup, start with `--dry-run` against a throwaway test
  calendar to validate titles, colors, durations, and timezones before
  pointing at the production shared calendar.

Notes & gotchas
---------------
- The pokedata.ovh API returns naive datetimes. The timezone is derived
  automatically from the provided LAT/LONG using timezonefinder.
- The API does not provide event durations; defaults are estimates and
  shown in the calendar description.
- "MISSING" events may be genuinely cancelled, postponed, or simply
  outside the search radius after a store relocation. Review before
  deleting.
- Calendar event IDs are derived from the event guid, so changing
  CALENDAR_ID and re-running effectively migrates the entire schedule
  to the new calendar — no manual export needed.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path

import discord
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from timezonefinder import TimezoneFinder


def load_dotenv_file(path=None):
    """Load key=value pairs from a .env file into os.environ (existing vars take priority)."""
    env_path = Path(path) if path else Path(__file__).parent / '.env'
    if not env_path.exists():
        return
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def resolve_timezone(lat, lng, fallback='America/New_York'):
    tz = TimezoneFinder().timezone_at(lat=lat, lng=lng)
    if not tz:
        print(f'Warning: could not determine timezone for ({lat}, {lng}), using {fallback}')
        return fallback
    return tz


# --- Location / search config ---
LAT = 0.0
LONG = 0.0

CUP_RADIUS = 140
CHALLENGE_RADIUS = 90
PRERELEASE_RADIUS = 50

# --- Local store ---
STORE_PATH = Path(__file__).parent / 'events_store.json'

# --- Google Calendar config ---
CALENDAR_ID = 'your_calendar_id@group.calendar.google.com'
SERVICE_ACCOUNT_FILE = Path(__file__).parent / 'google_api_credentials.json'
SCOPES = ['https://www.googleapis.com/auth/calendar']
EVENT_TZ = 'America/New_York'  # overridden at startup from LAT/LONG

# --- Discord config ---
LOCATION_NAME = 'Unknown'
DISCORD_WEBHOOK_URL = ''

# Default durations (hours) by event type, keyed off the API's `type` field.
DEFAULT_DURATIONS = {
    'League Cup': 4,
    'League Challenge': 3,
    'Prerelease': 3,
}
FALLBACK_DURATION_HOURS = 3

# Color IDs for Google Calendar events (named colors map to numeric IDs).
EVENT_COLOR_IDS = {
    'League Cup': '9',        # Blueberry
    'League Challenge': '7',  # Peacock
    'Prerelease': '6',        # Tangerine
}

# Fields we care about for detecting "meaningful" changes to an event.
TRACKED_FIELDS = ['name', 'date', 'time', 'when', 'cost', 'street_address', 'status', 'shop']

# Fields displayed when reviewing an event.
DISPLAY_FIELDS = ['type', 'shop', 'name', 'when', 'cost', 'street_address', 'city']


# ---------------------------------------------------------------------------
# Pokedata API
# ---------------------------------------------------------------------------

def fetch_events(tournament_type, radius):
    today = datetime.date.today()
    url = (
        f'https://www.pokedata.ovh/events/api/_tcg/{tournament_type}'
        f'/_latitude/{LAT}/_longitude/{LONG}/_radius/{radius}/_unit/mi/_start/{today}'
    )
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as e:
        print(f'Network error fetching {tournament_type}: {e}')
        return []

    if response.status_code != 200:
        print(f'Non-200 status fetching {tournament_type}: {response.status_code}')
        return []

    try:
        return response.json()
    except ValueError as e:
        print(f'Invalid JSON for {tournament_type}: {e}')
        return []


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/long points, in miles."""
    R = 3958.8
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return int(R * c)


def event_hash(event):
    payload = {k: (event.get(k) or '') for k in TRACKED_FIELDS}
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def snapshot(event):
    return {k: event.get(k) for k in TRACKED_FIELDS}


SMALL_WORDS = {'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'in',
               'of', 'on', 'or', 'the', 'to', 'vs', 'with'}
ACRONYMS = {'TCG', 'LLC', 'INC', 'USA', 'US', 'CCG', 'NC', 'SC', 'VA'}


def normalize_title_case(text):
    """Title-case a string, preserving acronyms and lowering small connector words."""
    if not text:
        return ''
    words = text.split()
    out = []
    for i, word in enumerate(words):
        upper = word.upper().strip('.,!?')
        if upper in ACRONYMS:
            out.append(word.upper())
        elif i != 0 and word.lower() in SMALL_WORDS:
            out.append(word.lower())
        else:
            out.append(word.capitalize())
    return ' '.join(out)


# ---------------------------------------------------------------------------
# Local store
# ---------------------------------------------------------------------------

def load_store():
    if not STORE_PATH.exists():
        return {}
    try:
        with STORE_PATH.open('r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'Could not read store at {STORE_PATH}: {e}. Starting fresh.')
        return {}


def save_store(store):
    tmp_path = STORE_PATH.with_suffix('.tmp')
    with tmp_path.open('w') as f:
        json.dump(store, f, indent=2, sort_keys=True)
    os.replace(tmp_path, STORE_PATH)


def prune_past_events(store, today_str):
    to_remove = []
    for guid, record in store.items():
        snap = record.get('snapshot') or {}
        when = snap.get('when') or ''
        event_date = when[:10]
        if event_date and event_date < today_str:
            to_remove.append(guid)
    for guid in to_remove:
        store.pop(guid, None)
    return len(to_remove)


def update_store_record(store, event, today_str):
    guid = event['guid']
    existing = store.get(guid, {})
    store[guid] = {
        'hash': event_hash(event),
        'snapshot': snapshot(event),
        'first_seen': existing.get('first_seen', today_str),
        'last_updated': today_str,
    }


# ---------------------------------------------------------------------------
# Classification + display
# ---------------------------------------------------------------------------

def cleaned_event_view(event):
    view = {k: event.get(k) for k in DISPLAY_FIELDS}
    contact = event.get('contact_data')
    view['details'] = contact.get('Details') if isinstance(contact, dict) else None
    try:
        view['distance (mi)'] = haversine(LAT, LONG, float(event['latitude']), float(event['longitude']))
    except (KeyError, ValueError, TypeError):
        view['distance (mi)'] = None
    return view


def diff_fields(prev_snapshot, current_event):
    changes = []
    for k in TRACKED_FIELDS:
        old = prev_snapshot.get(k)
        new = current_event.get(k)
        if (old or '') != (new or ''):
            changes.append((k, old, new))
    return changes


def classify_events(events, store):
    new_events = []
    updated_events = []
    unchanged_events = []
    seen_guids = set()

    for event in events:
        guid = event.get('guid')
        if not guid:
            guid = hashlib.sha1(
                f"{event.get('name')}|{event.get('when')}|{event.get('shop')}".encode()
            ).hexdigest()
            event['guid'] = guid

        seen_guids.add(guid)
        new_hash = event_hash(event)
        prev = store.get(guid)

        if prev is None:
            new_events.append(event)
        elif prev.get('hash') != new_hash:
            updated_events.append((event, prev))
        else:
            unchanged_events.append(event)

    today_str = datetime.date.today().isoformat()
    missing_events = []
    for guid, record in store.items():
        if guid in seen_guids:
            continue
        when = (record.get('snapshot') or {}).get('when') or ''
        event_date = when[:10]
        if event_date and event_date >= today_str:
            missing_events.append((guid, record))

    return new_events, updated_events, unchanged_events, missing_events


def review(event, label, prev_record=None):
    print('============')
    print(f'[{label}]')
    print(json.dumps(cleaned_event_view(event), indent=2))
    if prev_record is not None:
        changes = diff_fields(prev_record.get('snapshot') or {}, event)
        if changes:
            print('-- Changes --')
            for field, old, new in changes:
                print(f'  {field}: {old!r}  ->  {new!r}')


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

def get_calendar_service():
    if not SERVICE_ACCOUNT_FILE.exists():
        raise FileNotFoundError(f'Missing service account file at {SERVICE_ACCOUNT_FILE}')
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
    )
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


def gcal_event_id(guid):
    """
    Google Calendar event IDs must be base32hex chars (a-v, 0-9), 5-1024 long.
    Stripping dashes from a guid yields a valid hex string; prefix to namespace.
    """
    return 'pkmn' + guid.replace('-', '').lower()


def event_duration_hours(event):
    return DEFAULT_DURATIONS.get(event.get('type'), FALLBACK_DURATION_HOURS)


def to_gcal_event(event):
    when = event.get('when') or ''
    try:
        start = datetime.datetime.strptime(when, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        raise ValueError(f'Cannot parse "when" field: {when!r}')

    duration = event_duration_hours(event)
    end = start + datetime.timedelta(hours=duration)

    location = event.get('street_address') or event.get('city') or ''
    shop_raw = event.get('shop') or 'Unknown Shop'
    shop = normalize_title_case(shop_raw)
    event_type = event.get('type') or 'Event'
    event_type = event_type.replace('Pre Release', 'Prerelease')
    original_name = event.get('name') or ''
    cost = event.get('cost') or ''
    url = event.get('pokemon_url') or ''
    details = ''
    contact = event.get('contact_data')
    if isinstance(contact, dict):
        details = contact.get('Details') or ''

    # Calendar title: "<Shop> - <Event Type>"
    summary = f'{shop} - {event_type}'

    description_lines = []
    if original_name:
        description_lines.append(f'Listed as: {original_name}')
    description_lines.append(f'Shop: {shop}')
    description_lines.append(f'Type: {event_type}')
    description_lines.append(f'Cost: {cost}')
    description_lines.append(f'Duration: {duration} hours (estimated)')
    if url:
        description_lines.append(f'Pokemon.com: {url}')
    if details:
        description_lines.append('')
        description_lines.append(details)

    body = {
        'id': gcal_event_id(event['guid']),
        'summary': summary,
        'location': location,
        'description': '\n'.join(description_lines),
        'start': {'dateTime': start.isoformat(), 'timeZone': EVENT_TZ},
        'end': {'dateTime': end.isoformat(), 'timeZone': EVENT_TZ},
    }

    color_id = EVENT_COLOR_IDS.get(event_type)
    if color_id:
        body['colorId'] = color_id

    if url:
        body['source'] = {'title': 'pokedata.ovh', 'url': url}

    return body


def upsert_calendar_event(service, event):
    """Insert or update; returns 'inserted', 'updated', or 'skipped'."""
    try:
        body = to_gcal_event(event)
    except ValueError as e:
        print(f'  ! Could not build calendar event: {e}')
        return 'skipped'

    event_id = body['id']
    try:
        service.events().update(
            calendarId=CALENDAR_ID, eventId=event_id, body=body
        ).execute()
        return 'updated'
    except HttpError as e:
        if e.resp.status == 404:
            try:
                service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
                return 'inserted'
            except HttpError as insert_err:
                # A previously-deleted event with the same ID can return 409.
                if insert_err.resp.status == 409:
                    service.events().update(
                        calendarId=CALENDAR_ID, eventId=event_id, body=body
                    ).execute()
                    return 'updated'
                raise
        raise


def delete_calendar_event(service, guid):
    try:
        service.events().delete(
            calendarId=CALENDAR_ID, eventId=gcal_event_id(guid)
        ).execute()
        return True
    except HttpError as e:
        if e.resp.status in (404, 410):
            return False
        raise


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------

def _event_line(event):
    date = (event.get('when') or '')[:10]
    shop = normalize_title_case(event.get('shop') or 'Unknown')
    kind = event.get('type') or 'Event'
    return f'{date} — {shop} ({kind})'


def notify_discord(added_events, updated_events, unchanged_count, removed_events):
    if not DISCORD_WEBHOOK_URL:
        return

    if removed_events:
        color = 0xE67E22  # orange — cancellations
    elif added_events or updated_events:
        color = 0x3498DB  # blue — changes synced
    else:
        color = 0x2ECC71  # green — all quiet

    now = datetime.datetime.now(datetime.timezone.utc)
    embed = discord.Embed(
        title=f'Pokemon TCG Events — {LOCATION_NAME}',
        color=color,
        timestamp=now,
    )

    def _field(label, items, line_fn):
        if not items:
            return
        lines = [line_fn(e) for e in items[:20]]
        if len(items) > 20:
            lines.append(f'…and {len(items) - 20} more')
        embed.add_field(name=f'{label} ({len(items)})', value='\n'.join(lines), inline=False)

    _field('🆕 Added', added_events, _event_line)
    _field('🔄 Updated', updated_events, _event_line)
    _field('❌ Removed', removed_events, _event_line)

    embed.add_field(name='✅ Unchanged', value=str(unchanged_count), inline=True)

    radius_lines = (
        f'Cups: {CUP_RADIUS} mi\n'
        f'Challenges: {CHALLENGE_RADIUS} mi\n'
        f'Prereleases: {PRERELEASE_RADIUS} mi'
    )
    embed.add_field(name='📍 Search Center', value=f'{LAT}, {LONG}', inline=True)
    embed.add_field(name='🔭 Radii', value=radius_lines, inline=True)

    embed.set_footer(text='fetch_events.py')

    discord.SyncWebhook.from_url(DISCORD_WEBHOOK_URL).send(embed=embed)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def confirm(prompt, auto, default_yes=True):
    if auto:
        return default_yes
    try:
        ans = input(prompt).strip().lower()
    except EOFError:
        return default_yes
    if not ans:
        return default_yes
    return ans in ('y', 'yes')


def fetch(summary_only=False, auto=False, dry_run=False):
    store = load_store()
    today_str = datetime.date.today().isoformat()

    pruned = prune_past_events(store, today_str)
    if pruned:
        print(f'Pruned {pruned} past event(s) from local store')
        save_store(store)

    cups = fetch_events('cups', CUP_RADIUS)
    challenges = fetch_events('challenges', CHALLENGE_RADIUS)
    prereleases = fetch_events('pre', PRERELEASE_RADIUS)
    events = cups + challenges + prereleases
    events = sorted(events, key=lambda x: x.get('when') or '')

    new_events, updated_events, unchanged_events, missing_events = classify_events(events, store)

    print(f'Fetched {len(events)} events from API')
    print(f'  New:        {len(new_events)}')
    print(f'  Updated:    {len(updated_events)}')
    print(f'  Unchanged:  {len(unchanged_events)}')
    print(f'  Missing*:   {len(missing_events)}    (*previously seen, future-dated, not in this run)')
    print()

    if summary_only:
        for e in new_events:
            print(f'NEW       {e.get("when")}  {e.get("name")}  @ {e.get("shop")}')
        for e, _ in updated_events:
            print(f'UPDATED   {e.get("when")}  {e.get("name")}  @ {e.get("shop")}')
        for _, rec in missing_events:
            snap = rec.get('snapshot') or {}
            print(f'MISSING   {snap.get("when")}  {snap.get("name")}  @ {snap.get("shop")}')
        return

    service = None if dry_run else get_calendar_service()

    actually_added = []
    actually_updated = []
    actually_removed = []

    # Missing / possibly cancelled
    if missing_events:
        print('### Possibly cancelled / removed events ###')
        for i, (guid, record) in enumerate(missing_events, 1):
            snap = record.get('snapshot') or {}
            print('============')
            print(f'[POSSIBLY REMOVED] ({i}/{len(missing_events)})')
            print(json.dumps(snap, indent=2))
            if confirm(f'Remove from calendar and store? ({i}/{len(missing_events)}) [Y/n]: ', auto, default_yes=True):
                if not dry_run:
                    removed = delete_calendar_event(service, guid)
                    print(f'  -> calendar: {"deleted" if removed else "not found"}')
                else:
                    print('  -> [dry-run] would delete')
                store.pop(guid, None)
                save_store(store)
                actually_removed.append(snap)

    # New events
    if new_events:
        print('### New events ###')
        for i, event in enumerate(new_events, 1):
            review(event, 'NEW')
            if confirm(f'Add to calendar? ({i}/{len(new_events)}) [Y/n]: ', auto, default_yes=True):
                if not dry_run:
                    result = upsert_calendar_event(service, event)
                    print(f'  -> calendar: {result}')
                else:
                    print('  -> [dry-run] would insert')
                update_store_record(store, event, today_str)
                save_store(store)
                actually_added.append(event)

    # Updated events
    if updated_events:
        print('### Updated events ###')
        for i, (event, prev) in enumerate(updated_events, 1):
            review(event, 'UPDATED', prev_record=prev)
            if confirm(f'Update on calendar? ({i}/{len(updated_events)}) [Y/n]: ', auto, default_yes=True):
                if not dry_run:
                    result = upsert_calendar_event(service, event)
                    print(f'  -> calendar: {result}')
                else:
                    print('  -> [dry-run] would update')
                update_store_record(store, event, today_str)
                save_store(store)
                actually_updated.append(event)

    # Refresh hash/snapshot for unchanged so the store stays current
    for event in unchanged_events:
        guid = event['guid']
        if guid in store:
            store[guid]['snapshot'] = snapshot(event)
            store[guid]['hash'] = event_hash(event)
    save_store(store)

    if not dry_run:
        notify_discord(actually_added, actually_updated, len(unchanged_events), actually_removed)

    print('Done.')


def parse_args():
    p = argparse.ArgumentParser(description='Sync Pokemon TCG events to Google Calendar')
    p.add_argument('--summary', action='store_true', help='Print summary only, no calendar changes')
    p.add_argument('--auto', action='store_true', help='Skip confirmation prompts')
    p.add_argument('--dry-run', action='store_true', help='Do everything except call Google Calendar API')
    p.add_argument('--lat', type=float, help='Latitude of search center (overrides LAT env / default)')
    p.add_argument('--long', type=float, help='Longitude of search center (overrides LONG env / default)')
    p.add_argument('--calendar-id', dest='calendar_id', help='Google Calendar ID (overrides CALENDAR_ID env / default)')
    p.add_argument('--store', help='Path to local events store JSON (overrides STORE_PATH env / default)')
    p.add_argument('--cup-radius', dest='cup_radius', type=int, help='Search radius for League Cups in miles (overrides CUP_RADIUS env / default)')
    p.add_argument('--challenge-radius', dest='challenge_radius', type=int, help='Search radius for League Challenges in miles (overrides CHALLENGE_RADIUS env / default)')
    p.add_argument('--prerelease-radius', dest='prerelease_radius', type=int, help='Search radius for Pre Releases in miles (overrides PRERELEASE_RADIUS env / default)')
    p.add_argument('--env-file', help='Path to a .env file to load (default: .env next to this script)')
    p.add_argument('--credentials', help='Path to Google API service account JSON (overrides GOOGLE_API_CREDENTIALS env / default)')
    p.add_argument('--location-name', dest='location_name', help='Human-readable location label used in Discord notifications (overrides LOCATION_NAME env / default)')
    p.add_argument('--webhook-url', dest='webhook_url', help='Discord webhook URL for post-run notifications (overrides DISCORD_WEBHOOK_URL env / default)')
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()

    load_dotenv_file(args.env_file)

    # Apply config: CLI args > env vars > compiled-in defaults
    if args.lat is not None:
        LAT = args.lat
    elif 'LAT' in os.environ:
        LAT = float(os.environ['LAT'])

    if args.long is not None:
        LONG = args.long
    elif 'LONG' in os.environ:
        LONG = float(os.environ['LONG'])

    if args.calendar_id:
        CALENDAR_ID = args.calendar_id
    elif 'CALENDAR_ID' in os.environ:
        CALENDAR_ID = os.environ['CALENDAR_ID']

    if args.store:
        STORE_PATH = Path(args.store)
    elif 'STORE_PATH' in os.environ:
        STORE_PATH = Path(os.environ['STORE_PATH'])
    elif args.env_file:
        STORE_PATH = Path(args.env_file).parent / 'events_store.json'

    if args.cup_radius is not None:
        CUP_RADIUS = args.cup_radius
    elif 'CUP_RADIUS' in os.environ:
        CUP_RADIUS = int(os.environ['CUP_RADIUS'])

    if args.challenge_radius is not None:
        CHALLENGE_RADIUS = args.challenge_radius
    elif 'CHALLENGE_RADIUS' in os.environ:
        CHALLENGE_RADIUS = int(os.environ['CHALLENGE_RADIUS'])

    if args.prerelease_radius is not None:
        PRERELEASE_RADIUS = args.prerelease_radius
    elif 'PRERELEASE_RADIUS' in os.environ:
        PRERELEASE_RADIUS = int(os.environ['PRERELEASE_RADIUS'])

    if args.credentials:
        SERVICE_ACCOUNT_FILE = Path(args.credentials)
    elif 'GOOGLE_API_CREDENTIALS' in os.environ:
        SERVICE_ACCOUNT_FILE = Path(os.environ['GOOGLE_API_CREDENTIALS'])

    if args.location_name:
        LOCATION_NAME = args.location_name
    elif 'LOCATION_NAME' in os.environ:
        LOCATION_NAME = os.environ['LOCATION_NAME']

    if args.webhook_url:
        DISCORD_WEBHOOK_URL = args.webhook_url
    elif 'DISCORD_WEBHOOK_URL' in os.environ:
        DISCORD_WEBHOOK_URL = os.environ['DISCORD_WEBHOOK_URL']

    EVENT_TZ = resolve_timezone(LAT, LONG)

    fetch(summary_only=args.summary, auto=args.auto, dry_run=args.dry_run)
