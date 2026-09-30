#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
AGGREGATOR_DIR="$PROJECT_DIR/signal_aggregator"
AGGREGATOR_URL="${SIGNAL_DAILYHOT_BASE_URL:-http://127.0.0.1:3210}"

if ! curl -fsS --max-time 3 "$AGGREGATOR_URL/zhihu" >/dev/null 2>&1; then
  cd "$AGGREGATOR_DIR"
  if test ! -d node_modules; then
    npm install
  fi
  nohup npm start > "$PROJECT_DIR/logs/signal_aggregator.log" 2>&1 &

  attempts=0
  until curl -fsS --max-time 3 "$AGGREGATOR_URL/zhihu" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if test "$attempts" -ge 15; then
      echo "信号聚合服务启动失败，请查看 logs/signal_aggregator.log" >&2
      exit 1
    fi
    sleep 1
  done
fi

cd "$PROJECT_DIR"
exec .venv/bin/python signal_source_probe.py "$@"
