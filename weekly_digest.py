"""
Weekly Pokemon TCG Event Digest
================================

Reads upcoming events from one or more Google Calendars and posts a combined
text-calendar to Discord for the next N days (default: 7).

Reads CALENDAR_ID, LOCATION_NAME, and optionally GOOGLE_API_CREDENTIALS and
DISCORD_WEBHOOK_URL from each .env file. DISCORD_WEBHOOK_URL is taken from
the first .env file that defines it, or from --webhook-url / the environment.

Usage
-----
Single calendar:
    python weekly_digest.py

Multiple calendars:
    python weekly_digest.py --env-files raleigh.env charlotte.env wilmington.env

Preview without sending:
    python weekly_digest.py --env-files *.env --dry-run

Look ahead two weeks:
    python weekly_digest.py --env-files *.env --days 14
"""

import argparse
import datetime
import html
import json
import os
from pathlib import Path

import discord
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


DEFAULT_CREDENTIALS = Path(__file__).parent / 'google_api_credentials.json'
DEFAULT_STORE_MENTIONS = Path(__file__).parent / 'store_mentions.json'


def load_store_mentions(path):
    """
    Return {normalized_shop_name: mention_string} from a JSON config, or {} if not found.
    Values should be Discord mention strings: '<@&ROLE_ID>' for roles, '<#CHANNEL_ID>' for channels.
    """
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        data = json.load(f)
    return {k.lower(): v for k, v in data.items()}


def shop_from_summary(summary):
    """Extract shop name from 'Shop Name - Event Type' calendar summary."""
    return summary.split(' - ')[0].strip()


def event_type_from_summary(summary):
    shop = shop_from_summary(summary)
    return summary[len(shop):].lstrip(' -').strip()


def event_fingerprint(event):
    """Stable key for deduplication: same shop + event type + start time = same event."""
    summary = event.get('summary', '')
    shop = shop_from_summary(summary).lower()
    etype = event_type_from_summary(summary).lower()
    start = event.get('start', {})
    dt_str = (start.get('dateTime') or start.get('date') or '')[:19]
    return (shop, etype, dt_str)


def dedup_events(day_events):
    """
    Merge events that share the same fingerprint across calendars.
    Returns list of (locations, event) sorted by start time.
    """
    merged = {}
    order = []
    for location, event in day_events:
        fp = event_fingerprint(event)
        if fp in merged:
            merged[fp][0].append(location)
        else:
            merged[fp] = ([location], event)
            order.append(fp)
    return [merged[fp] for fp in order]



def load_env_file(path):
    """Return a dict of key/value pairs from a .env file."""
    env_path = Path(path)
    if not env_path.exists():
        return {}
    cfg = {}
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


def get_calendar_service(credentials_path):
    creds = service_account.Credentials.from_service_account_file(
        str(credentials_path),
        scopes=['https://www.googleapis.com/auth/calendar.readonly'],
    )
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


def fetch_calendar_events(service, calendar_id, start_dt, end_dt):
    try:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy='startTime',
        ).execute()
        return result.get('items', [])
    except HttpError as e:
        print(f'  Error fetching {calendar_id}: {e}')
        return []


def event_start_dt(gcal_event):
    start = gcal_event.get('start', {})
    if 'dateTime' in start:
        return datetime.datetime.fromisoformat(start['dateTime'])
    if 'date' in start:
        d = datetime.date.fromisoformat(start['date'])
        return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
    return None


def event_date(gcal_event):
    dt = event_start_dt(gcal_event)
    return dt.date() if dt else None


def event_time_str(gcal_event):
    start = gcal_event.get('start', {})
    if 'dateTime' in start:
        dt = datetime.datetime.fromisoformat(start['dateTime'])
        return dt.strftime('%-I:%M %p')
    return 'All day'


def build_calendar_text(all_events, start_date, days, store_mentions=None):
    store_mentions = store_mentions or {}
    by_date = {start_date + datetime.timedelta(days=i): [] for i in range(days)}

    for location, event in all_events:
        start = event.get('start', {})
        if 'dateTime' not in start and 'date' in start:
            # All-day event: expand across every day it covers (end.date is exclusive)
            start_d = datetime.date.fromisoformat(start['date'])
            end_raw = event.get('end', {}).get('date')
            end_d = datetime.date.fromisoformat(end_raw) if end_raw else start_d + datetime.timedelta(days=1)
            d = start_d
            while d < end_d:
                if d in by_date:
                    by_date[d].append((location, event))
                d += datetime.timedelta(days=1)
        else:
            d = event_date(event)
            if d in by_date:
                by_date[d].append((location, event))

    lines = []
    for d in sorted(by_date):
        lines.append(f'**{d.strftime("%A, %b %-d")}**')
        _epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        day_events = sorted(by_date[d], key=lambda x: event_start_dt(x[1]) or _epoch)
        if day_events:
            for locations, event in dedup_events(day_events):
                summary = html.unescape(event.get('summary', 'Event'))
                shop = shop_from_summary(summary)
                event_type = event_type_from_summary(summary)
                mention = store_mentions.get(shop.lower())
                shop_str = mention if mention else shop
                location_label = ', '.join(locations)
                time = event_time_str(event)
                suffix = f' @ {time}' if time != 'All day' else ''
                label = f'{shop_str} - {event_type}' if event_type else shop_str
                lines.append(f'• [{location_label}] {label}{suffix}')
        else:
            lines.append('*(no events)*')
        lines.append('')

    return lines


def chunk_lines(header, lines, limit=2000):
    """
    Yield message strings, each within `limit` chars.
    The header is prepended only to the first chunk.
    Splits at blank lines (day boundaries) where possible.
    """
    current = header
    for line in lines:
        candidate = current + '\n' + line if current else line
        if len(candidate) > limit:
            yield current
            current = line
        else:
            current = candidate
    if current:
        yield current


def send_discord(webhook_url, lines, start_date, days, dry_run=False):
    end_date = start_date + datetime.timedelta(days=days - 1)
    date_range = f'{start_date.strftime("%b %-d")} to {end_date.strftime("%b %-d")}'
    header = f'📅 **Pokemon TCG — {date_range}**'

    chunks = list(chunk_lines(header, lines))

    if dry_run:
        for i, chunk in enumerate(chunks, 1):
            print(f'[dry-run] message {i}/{len(chunks)}:')
            print(chunk)
            print()
        return

    webhook = discord.SyncWebhook.from_url(webhook_url)
    for chunk in chunks:
        webhook.send(
            content=chunk,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    print(f'Discord notification sent ({len(chunks)} message(s)).')


def parse_args():
    p = argparse.ArgumentParser(description='Send a weekly Pokemon TCG event digest to Discord')
    p.add_argument(
        '--env-files', nargs='+', default=['.env'],
        help='One or more .env files to read (default: .env)',
    )
    p.add_argument('--webhook-url', help='Discord webhook URL (overrides env DISCORD_WEBHOOK_URL)')
    p.add_argument('--credentials', help='Path to Google API service account JSON (overrides GOOGLE_API_CREDENTIALS in env files)')
    p.add_argument('--days', type=int, default=7, help='Days to look ahead (default: 7)')
    p.add_argument('--store-mentions', default=str(DEFAULT_STORE_MENTIONS),
                   help='JSON file mapping shop names to Discord mention strings (default: store_mentions.json)')
    p.add_argument('--dry-run', action='store_true', help='Print the calendar without sending to Discord')
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()

    today = datetime.date.today()
    start_dt = datetime.datetime.combine(today, datetime.time.min, tzinfo=datetime.timezone.utc)
    end_dt = start_dt + datetime.timedelta(days=args.days)

    webhook_url = args.webhook_url or os.environ.get('DISCORD_WEBHOOK_URL', '')
    all_events = []

    for env_file in args.env_files:
        cfg = load_env_file(env_file)

        calendar_id = cfg.get('CALENDAR_ID', '')
        location = cfg.get('LOCATION_NAME', Path(env_file).stem)
        credentials_path = Path(args.credentials or cfg.get('GOOGLE_API_CREDENTIALS', str(DEFAULT_CREDENTIALS)))

        if not webhook_url:
            webhook_url = cfg.get('DISCORD_WEBHOOK_URL', '')

        if not calendar_id or calendar_id == 'your_calendar_id@group.calendar.google.com':
            print(f'Skipping {env_file}: no CALENDAR_ID configured')
            continue
        if not credentials_path.exists():
            print(f'Skipping {env_file}: credentials not found at {credentials_path}')
            continue

        print(f'Fetching {location}…')
        try:
            service = get_calendar_service(credentials_path)
            events = fetch_calendar_events(service, calendar_id, start_dt, end_dt)
            print(f'  {len(events)} event(s)')
            for event in events:
                all_events.append((location, event))
        except Exception as e:
            print(f'  Error: {e}')

    store_mentions = load_store_mentions(args.store_mentions)
    if store_mentions:
        print(f'Loaded {len(store_mentions)} store mention(s) from {args.store_mentions}')

    lines = build_calendar_text(all_events, today, args.days, store_mentions=store_mentions)

    if not webhook_url and not args.dry_run:
        print('No Discord webhook URL found. Pass --webhook-url or set DISCORD_WEBHOOK_URL in an .env file.')
        print()
        print('\n'.join(lines))
    else:
        send_discord(webhook_url, lines, today, args.days, dry_run=args.dry_run)
