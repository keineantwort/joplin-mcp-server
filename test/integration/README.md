# Integration Tests

Tests against a real Joplin Server instance using a temporary local Joplin CLI.

## Setup

### 1. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Joplin Server credentials
```

See [.env.example](.env.example) for all available options, including:

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `JOPLIN_SERVER_URL` | yes | Joplin Server URL (e.g. `http://host:22300`) |
| `JOPLIN_SERVER_USER` | yes | Account email |
| `JOPLIN_SERVER_PASSWORD` | yes | Account password |
| `JOPLIN_ENCRYPTION_PASSWORD` | no | E2EE master key password — if set, encrypted notes are decrypted after sync |
| `JOPLIN_PORT` | no | Data API port (default: `41184`) |
| `JOPLIN_TOKEN` | no | Fixed API token (default: random) |

### 2. Start the local API

```bash
./setup_local_cli.sh
```

Without a `.env` file the script prompts interactively for credentials.

This will:
1. Install Joplin CLI into `.local-cli/` (auto-removed on exit)
2. Sync with your Joplin Server
3. Decrypt E2EE data (if `JOPLIN_ENCRYPTION_PASSWORD` is set)
4. Start the Data API on `localhost:$JOPLIN_PORT`
5. Print connection details (`JOPLIN_TOKEN`, port)

Press `Ctrl-C` to stop and clean up.

## Scripts

| Script | Description |
|--------|-------------|
| `diagnose_notebooks.py` | Show the full notebook tree, analyze filter visibility, and count notes |

## Running

In a second terminal, with the API running:

```bash
# List all notebooks
JOPLIN_TOKEN=<token from setup output> python test/integration/diagnose_notebooks.py

# Simulate a notebook filter
JOPLIN_TOKEN=<token> python test/integration/diagnose_notebooks.py --filter "Work,Personal"
```
