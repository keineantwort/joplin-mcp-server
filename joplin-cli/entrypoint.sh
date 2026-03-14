#!/bin/sh
set -e

echo "=== Joplin CLI Daemon ==="
echo "Configuring sync target: Joplin Server"

# Configure sync to Joplin Server (target 9)
joplin config sync.target 9
joplin config sync.9.path "${JOPLIN_SERVER_URL}"
joplin config sync.9.username "${JOPLIN_SERVER_USER}"
joplin config sync.9.password "${JOPLIN_SERVER_PASSWORD}"

# Configure API token for the Data API
# Joplin CLI binds only to 127.0.0.1, so we use an internal port
# and socat to expose it on 0.0.0.0:41184 for other containers
joplin config api.token "${JOPLIN_TOKEN}"
joplin config api.port 41185

# Disable keychain (not available in container)
joplin config keychain.supported 0

# Initial sync before starting the API server (needed before E2EE setup)
echo "Running initial sync..."
joplin sync || echo "Initial sync failed (may succeed on retry)"

# E2EE: decrypt notes BEFORE starting the API so encrypted notebooks are
# visible immediately. The --password flag is ignored by the CLI, so we
# pipe the password via stdin instead.
if [ -n "${JOPLIN_ENCRYPTION_PASSWORD}" ]; then
    echo "Decrypting E2EE data (this may take a while)..."
    # printf is safe for passwords with special characters (unlike echo).
    # The while loop ensures the password is available for multiple master key prompts.
    (while true; do printf '%s\n' "${JOPLIN_ENCRYPTION_PASSWORD}"; sleep 0.1; done) \
        | joplin e2ee decrypt --retry-failed-items 2>&1 || true
    echo "E2EE decryption complete."
fi

# Start the API server in the background (binds to 127.0.0.1:41185)
echo "Starting Joplin API server on internal port 41185..."
joplin server start &

# Wait for API to become available
echo "Waiting for API server..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:41185/ping > /dev/null 2>&1; then
        echo "API server is ready!"
        break
    fi
    sleep 2
done

# Expose API on 0.0.0.0:41184 via socat (Joplin CLI only binds to localhost)
echo "Starting socat proxy 0.0.0.0:41184 -> 127.0.0.1:41185..."
socat TCP-LISTEN:41184,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:41185 &

# --- Sync trigger endpoints ---
# Port 41186: async (fire-and-forget) — used after write operations
# Port 41187: blocking — waits for sync to complete, used by sync_notes tool

cat > /sync-async.sh <<'SYNCASYNC'
#!/bin/sh
while IFS= read -r line; do
    line="${line%%$(printf '\r')}"
    [ -z "$line" ] && break
done
if [ ! -f /tmp/sync.lock ]; then
    (
        touch /tmp/sync.lock
        joplin sync >/dev/null 2>&1
        rm -f /tmp/sync.lock
    ) &
fi
printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 3\r\n\r\nok\n'
SYNCASYNC
chmod +x /sync-async.sh

cat > /sync-blocking.sh <<'SYNCBLOCKING'
#!/bin/sh
while IFS= read -r line; do
    line="${line%%$(printf '\r')}"
    [ -z "$line" ] && break
done
# Wait if another sync is already running
while [ -f /tmp/sync.lock ]; do
    sleep 1
done
touch /tmp/sync.lock
if joplin sync 2>&1; then
    MSG='{"status":"success"}'
else
    MSG='{"status":"error","message":"sync failed"}'
fi
rm -f /tmp/sync.lock
LEN=$(printf '%s' "$MSG" | wc -c)
printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s' "$LEN" "$MSG"
SYNCBLOCKING
chmod +x /sync-blocking.sh

echo "Starting sync trigger endpoints (async=41186, blocking=41187)..."
socat TCP-LISTEN:41186,fork,reuseaddr EXEC:/sync-async.sh &
socat TCP-LISTEN:41187,fork,reuseaddr EXEC:/sync-blocking.sh &

# Periodic sync loop (also re-decrypts if new encrypted items arrive)
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"
echo "Starting sync loop (interval: ${SYNC_INTERVAL}s)"
while true; do
    sleep "${SYNC_INTERVAL}"
    echo "$(date): Running sync..."
    joplin sync || echo "Sync failed, will retry next interval"
    if [ -n "${JOPLIN_ENCRYPTION_PASSWORD}" ]; then
        (while true; do printf '%s\n' "${JOPLIN_ENCRYPTION_PASSWORD}"; sleep 0.1; done) \
            | joplin e2ee decrypt --retry-failed-items 2>&1 || true
    fi
done
