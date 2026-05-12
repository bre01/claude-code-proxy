#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE="./kimi-gateway.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "no pid file. Not running?"
    exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    for _ in 1 2 3 4 5; do
        if ! kill -0 "$PID" 2>/dev/null; then break; fi
        sleep 0.4
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "stubborn, sending SIGKILL"
        kill -9 "$PID" || true
    fi
    echo "stopped pid $PID"
else
    echo "pid $PID not running, cleaning up"
fi
rm -f "$PID_FILE"
