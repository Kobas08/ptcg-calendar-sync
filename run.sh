#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/venv/bin/activate"

python "$SCRIPT_DIR/fetch_events.py" --env-file "$SCRIPT_DIR/rdu.env" "$@"
python "$SCRIPT_DIR/fetch_events.py" --env-file "$SCRIPT_DIR/charlotte.env" "$@"
python "$SCRIPT_DIR/fetch_events.py" --env-file "$SCRIPT_DIR/wilmington.env" "$@"
python "$SCRIPT_DIR/fetch_events.py" --env-file "$SCRIPT_DIR/greensboro.env" "$@"
