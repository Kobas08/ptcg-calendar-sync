#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/venv/bin/activate"

python "$SCRIPT_DIR/fetch_events.py" --env-file "$SCRIPT_DIR/raleigh.env" "$@"
# python "$SCRIPT_DIR/fetch_events.py" --env-file "$SCRIPT_DIR/charlotte.env" "$@"
