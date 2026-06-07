#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Creating virtual environment..."
virtualenv -p python3.11 "$SCRIPT_DIR/venv"

echo "Installing requirements..."
"$SCRIPT_DIR/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "Setup complete. Add this to your crontab (crontab -e):"
echo ""
echo "0 9 * * 0 $SCRIPT_DIR/run.sh --auto >> $SCRIPT_DIR/events.log 2>&1"
