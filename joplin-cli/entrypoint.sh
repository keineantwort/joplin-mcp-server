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

# Initial sync before starting the API server (needed before E2EE setup)
echo "Running initial sync..."
joplin sync || echo "Initial sync failed (may succeed on retry)"

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

# E2EE: decrypt notes in the background (non-blocking, API is already up)
if [ -n "${JOPLIN_E2EE_PASSWORD}" ]; then
    echo "Starting E2EE decryption in background..."
    joplin e2ee decrypt --password "${JOPLIN_E2EE_PASSWORD}" &
fi

# Periodic sync loop
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"
echo "Starting sync loop (interval: ${SYNC_INTERVAL}s)"
while true; do
    sleep "${SYNC_INTERVAL}"
    echo "$(date): Running sync..."
    joplin sync || echo "Sync failed, will retry next interval"
done
