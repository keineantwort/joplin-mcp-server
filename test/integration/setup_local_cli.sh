#!/usr/bin/env bash
# --------------------------------------------------------------------------
# setup_local_cli.sh — Install Joplin CLI into a temporary directory, sync
# it against a real Joplin Server, and start the Data API.
#
# This gives you a local Joplin Data API (port 41184) backed by real data
# without Docker, perfect for running the diagnostic and test scripts.
#
# Requirements: node >= 18, npm
#
# Usage:
#   # Interactive — prompts for server URL, user, password:
#   ./setup_local_cli.sh
#
#   # Non-interactive via environment:
#   JOPLIN_SERVER_URL=http://joplin.example.com:22300 \
#   JOPLIN_SERVER_USER=you@example.com \
#   JOPLIN_SERVER_PASSWORD=secret \
#     ./setup_local_cli.sh
#
# The script prints connection details (host, port, token) when the API is
# ready.  Press Ctrl-C to stop and clean up.
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$SCRIPT_DIR/.local-cli"

# --- Load .env if present ---
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
    echo "Loaded config from $SCRIPT_DIR/.env"
fi

JOPLIN_PORT="${JOPLIN_PORT:-41184}"
JOPLIN_TOKEN="${JOPLIN_TOKEN:-$(openssl rand -hex 32)}"

# --- Check for conflicting Joplin Desktop instance ---
if lsof -i :"$JOPLIN_PORT" > /dev/null 2>&1; then
    BLOCKER=$(lsof -i :"$JOPLIN_PORT" -t 2>/dev/null | head -1)
    BLOCKER_NAME=$(ps -p "$BLOCKER" -o comm= 2>/dev/null || echo "unknown")
    echo "ERROR: Port $JOPLIN_PORT is already in use by $BLOCKER_NAME (PID $BLOCKER)." >&2
    echo >&2
    echo "This is likely Joplin Desktop. Please either:" >&2
    echo "  1. Quit Joplin Desktop before running this script" >&2
    echo "  2. Use a different port: JOPLIN_PORT=41185 $0" >&2
    exit 1
fi

# --- Prompt for missing credentials ---
if [[ -z "${JOPLIN_SERVER_URL:-}" ]]; then
    read -rp "Joplin Server URL (e.g. http://host:22300): " JOPLIN_SERVER_URL
fi
if [[ -z "${JOPLIN_SERVER_USER:-}" ]]; then
    read -rp "Joplin Server User (email): " JOPLIN_SERVER_USER
fi
if [[ -z "${JOPLIN_SERVER_PASSWORD:-}" ]]; then
    read -rsp "Joplin Server Password: " JOPLIN_SERVER_PASSWORD
    echo
fi
if [[ -z "${JOPLIN_ENCRYPTION_PASSWORD:-}" ]]; then
    read -rsp "Encryption Password (leave empty if not using E2EE): " JOPLIN_ENCRYPTION_PASSWORD
    echo
fi

echo "============================================================"
echo "Joplin CLI — local setup"
echo "============================================================"
echo "Server:     $JOPLIN_SERVER_URL"
echo "User:       $JOPLIN_SERVER_USER"
echo "Work dir:   $WORK_DIR"
echo

# --- Cleanup on exit ---
cleanup() {
    echo
    echo "Shutting down..."
    if [[ -n "${JOPLIN_PID:-}" ]] && kill -0 "$JOPLIN_PID" 2>/dev/null; then
        kill "$JOPLIN_PID" 2>/dev/null || true
        wait "$JOPLIN_PID" 2>/dev/null || true
        echo "  Joplin API server stopped."
    fi
    if [[ -d "$WORK_DIR" ]]; then
        rm -rf "$WORK_DIR"
        echo "  Work directory removed."
    fi
    echo "Done."
}
trap cleanup EXIT

# --- Install Joplin CLI ---
echo ">> Installing Joplin CLI (this may take a minute)..."
mkdir -p "$WORK_DIR"

# Create a minimal package.json directly (avoids npm init issues)
cat > "$WORK_DIR/package.json" <<'PKGJSON'
{"name":"joplin-cli-test","version":"1.0.0","private":true}
PKGJSON

# npm install can produce warnings — don't let them abort the script
set +e
npm install --prefix "$WORK_DIR" joplin 2>&1
NPM_EXIT=$?
set -e
if [[ $NPM_EXIT -ne 0 ]]; then
    echo "ERROR: npm install joplin failed (exit $NPM_EXIT)" >&2
    exit 1
fi
JOPLIN="$WORK_DIR/node_modules/.bin/joplin"

if [[ ! -x "$JOPLIN" ]]; then
    echo "ERROR: Joplin CLI binary not found at $JOPLIN" >&2
    exit 1
fi
echo "  Installed: $($JOPLIN version 2>/dev/null || echo 'unknown version')"

# Isolate Joplin data: override HOME so ~/.config/joplin stays inside WORK_DIR.
# The --profile flag is unreliable in the npm version of the CLI.
REAL_HOME="$HOME"
export HOME="$WORK_DIR/home"
mkdir -p "$HOME/.config"

# Prevent Joplin from accessing macOS Keychain by stubbing out the
# keytar native module with a no-op JS implementation.
# The real entry point is keytar/lib/keytar.js which loads a native .node binary.
KEYTAR_MAIN="$WORK_DIR/node_modules/keytar/lib/keytar.js"
if [[ -f "$KEYTAR_MAIN" ]]; then
    cat > "$KEYTAR_MAIN" <<'KEYTAR_STUB'
module.exports = {
    getPassword: async () => null,
    setPassword: async () => {},
    deletePassword: async () => true,
    findPassword: async () => null,
    findCredentials: async () => [],
};
KEYTAR_STUB
    echo "  Keychain access disabled (keytar stubbed)."
fi

# Write settings.json directly — avoids issues with `joplin config` under
# a fake HOME (missing keychain, XDG errors, etc.)
# Uses python to properly JSON-encode values with special characters.
mkdir -p "$HOME/.config/joplin"
python3 -c "
import json, sys
settings = {
    'sync.target': 9,
    'sync.9.path': sys.argv[1],
    'sync.9.username': sys.argv[2],
    'sync.9.password': sys.argv[3],
    'api.token': sys.argv[4],
    'api.port': int(sys.argv[5]),
    'keychain.supported': 0,
}
json.dump(settings, open(sys.argv[6], 'w'), indent=4)
" "$JOPLIN_SERVER_URL" "$JOPLIN_SERVER_USER" "$JOPLIN_SERVER_PASSWORD" \
  "$JOPLIN_TOKEN" "$JOPLIN_PORT" "$HOME/.config/joplin/settings.json"

echo
echo ">> Sync configured via settings.json"

# --- Sync ---
echo
echo ">> Initial sync with Joplin Server (this may take a while)..."
"$JOPLIN" sync 2>&1
echo "  Initial sync complete."

# --- Decrypt E2EE if encryption password was provided ---
if [[ -n "${JOPLIN_ENCRYPTION_PASSWORD:-}" ]]; then
    echo
    echo ">> Decrypting E2EE data..."
    # Feed the password to stdin for each master key prompt.
    # Temporarily disable pipefail — the password feeder exits with
    # SIGPIPE once joplin closes stdin, which is expected.
    set +o pipefail
    python3 -c "
import sys, signal, time
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
pw = sys.argv[1] + '\n'
while True:
    sys.stdout.write(pw)
    sys.stdout.flush()
    time.sleep(0.1)
" "$JOPLIN_ENCRYPTION_PASSWORD" 2>/dev/null | "$JOPLIN" e2ee decrypt --retry-failed-items 2>&1
    set -o pipefail
fi

# --- Start API server ---
echo
echo ">> Starting Joplin Data API..."
"$JOPLIN" server start &
JOPLIN_PID=$!

echo -n "  Waiting for API"
for _ in $(seq 1 30); do
    if curl -sf "http://localhost:$JOPLIN_PORT/ping?token=$JOPLIN_TOKEN" > /dev/null 2>&1; then
        echo " ready!"
        break
    fi
    echo -n "."
    sleep 1
done

if ! curl -sf "http://localhost:$JOPLIN_PORT/ping?token=$JOPLIN_TOKEN" > /dev/null 2>&1; then
    echo " FAILED" >&2
    echo "ERROR: Joplin API did not become ready within 30s" >&2
    exit 1
fi

# --- Print connection info ---
echo
echo "============================================================"
echo "Joplin Data API is running"
echo "============================================================"
echo "  JOPLIN_HOST=localhost"
echo "  JOPLIN_PORT=$JOPLIN_PORT"
echo "  JOPLIN_TOKEN=$JOPLIN_TOKEN"
echo
echo "Run tests in another terminal, e.g.:"
echo "  JOPLIN_TOKEN=$JOPLIN_TOKEN python test/integration/diagnose_notebooks.py"
echo
echo "Press Ctrl-C to stop and clean up."
echo "============================================================"

# Keep running until interrupted
wait "$JOPLIN_PID"
