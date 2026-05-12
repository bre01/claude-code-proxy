#!/bin/bash
# Start kimi-gateway as a detached daemon.
# Reads KIMI_KEY from .env if present, or expects it in the environment.
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE="./kimi-gateway.pid"
LOG_FILE="./kimi-gateway.log"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "kimi-gateway already running (pid $(cat "$PID_FILE")). Use ./stop.sh first."
    exit 1
fi

if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

if [[ -z "${KIMI_KEY:-}" ]]; then
    echo "ERROR: set KIMI_KEY in .env or env before starting."
    exit 1
fi

# Auto-detect mkcert-generated certs in ./certs/ and serve over HTTPS if present.
if [[ -z "${SSL_CERTFILE:-}" && -f "./certs/cert.pem" && -f "./certs/key.pem" ]]; then
    export SSL_CERTFILE="$(pwd)/certs/cert.pem"
    export SSL_KEYFILE="$(pwd)/certs/key.pem"
fi

nohup uv run python server.py >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
disown || true

sleep 0.6
PORT_USED="${PORT:-8765}"
HOST_USED="${BIND_HOST:-127.0.0.1}"
SCHEME="http"
CURL_OPTS=""
if [[ -n "${SSL_CERTFILE:-}" ]]; then
    SCHEME="https"
    CURL_OPTS="-k"  # cert may not be trusted by this shell's user CA bundle
fi
if curl -fsS $CURL_OPTS "${SCHEME}://${HOST_USED}:${PORT_USED}/healthz" >/dev/null 2>&1; then
    echo "kimi-gateway started: pid $(cat "$PID_FILE"), ${SCHEME}://${HOST_USED}:${PORT_USED}"
    echo "log: $(pwd)/$LOG_FILE"
else
    echo "kimi-gateway pid $(cat "$PID_FILE") launched but /healthz did not respond yet."
    echo "Check log: $(pwd)/$LOG_FILE"
fi
