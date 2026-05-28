#!/bin/bash
set -e

# Path to the cloned ptcg-calendar-sync repo
CODE_DIR="/path/to/ptcg-calendar-sync"

# This script lives in the configs repo alongside the .env files
CONF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDENTIALS="$CONF_DIR/../shared/google_api_credentials.json"

source "$CODE_DIR/venv/bin/activate"

python "$CODE_DIR/fetch_events.py" --credentials "$CREDENTIALS" --env-file "$CONF_DIR/location1.env" "$@"
python "$CODE_DIR/fetch_events.py" --credentials "$CREDENTIALS" --env-file "$CONF_DIR/location2.env" "$@"
