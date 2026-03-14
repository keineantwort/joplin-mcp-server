# Integration Tests

Tests against a real Joplin Server instance using a temporary local Joplin CLI.

## Setup

```bash
# Start a temporary Joplin CLI with Data API (interactive prompts):
./setup_local_cli.sh

# Or non-interactive:
JOPLIN_SERVER_URL=http://host:22300 \
JOPLIN_SERVER_USER=you@example.com \
JOPLIN_SERVER_PASSWORD=secret \
  ./setup_local_cli.sh
```

This will:
1. Install Joplin CLI into `.local-cli/` (auto-removed on exit)
2. Sync with your Joplin Server
3. Start the Data API on port 41184
4. Print the `JOPLIN_TOKEN` to use with the test scripts

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
