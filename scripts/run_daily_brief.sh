#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_push.log"
PYTHON="$PROJECT_DIR/.venv/bin/python"

mkdir -p "$LOG_DIR"

{
  echo ""
  echo "===== AI Frontier daily push started at $(date '+%Y-%m-%d %H:%M:%S %z') ====="

  cd "$PROJECT_DIR"

  if [ ! -x "$PYTHON" ]; then
    echo "Python not found: $PYTHON"
    exit 1
  fi

  "$PROJECT_DIR/scripts/run_signal_probe.sh" \
    --output data/signal_probe/latest.json

  if [ "${AI_FRONTIER_DRY_RUN:-0}" = "1" ]; then
    "$PYTHON" daily_brief.py --dry-run
  else
    "$PYTHON" daily_brief.py
  fi

  echo "===== AI Frontier daily push finished at $(date '+%Y-%m-%d %H:%M:%S %z') ====="
} >> "$LOG_FILE" 2>&1
