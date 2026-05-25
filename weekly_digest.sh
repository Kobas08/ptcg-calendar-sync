#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/venv/bin/activate"

python "$SCRIPT_DIR/weekly_digest.py" \
    --env-files \
        "$SCRIPT_DIR/meta.env" \
        "$SCRIPT_DIR/rdu.env" \
        "$SCRIPT_DIR/charlotte.env" \
        "$SCRIPT_DIR/wilmington.env" \
        "$SCRIPT_DIR/greensboro.env" \
    "$@"
