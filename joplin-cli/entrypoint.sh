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
joplin config api.token "${JOPLIN_TOKEN}"
joplin config api.port 41184

# Initial sync before starting the API server (needed before E2EE setup)
echo "Running initial sync..."
joplin sync || echo "Initial sync failed (may succeed on retry)"

# E2EE: decrypt notes if encryption is enabled on the server
if [ -n "${JOPLIN_E2EE_PASSWORD}" ]; then
    echo "Configuring E2EE decryption..."
    joplin e2ee decrypt --password "${JOPLIN_E2EE_PASSWORD}"
    echo "Running post-E2EE sync..."
    joplin sync || echo "Post-E2EE sync failed (may succeed on retry)"
fi

# Start the API server in the background
echo "Starting Joplin API server on port 41184..."
joplin server start &

# Wait for API to become available
echo "Waiting for API server..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:41184/ping > /dev/null 2>&1; then
        echo "API server is ready!"
        break
    fi
    sleep 2
done

# Periodic sync loop
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"
echo "Starting sync loop (interval: ${SYNC_INTERVAL}s)"
while true; do
    sleep "${SYNC_INTERVAL}"
    echo "$(date): Running sync..."
    joplin sync || echo "Sync failed, will retry next interval"
done
