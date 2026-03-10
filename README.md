# Joplin MCP Server — Docker Edition

A Docker-based [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives Claude.ai access to a self-hosted [Joplin](https://joplinapp.org/) note collection via Streamable HTTP transport.

Fork of [dweigend/joplin-mcp-server](https://github.com/dweigend/joplin-mcp-server), extended with remote HTTP transport and Docker deployment.

## Architecture

```
Claude.ai ──HTTPS──► Reverse Proxy (NPMPlus)
                            │
                            │ :8000 (internal)
                            ▼
                    [Container: joplin-mcp]
                    Python FastMCP Server
                    Streamable HTTP Transport
                            │
                            │ :41184 (internal Docker network)
                            ▼
                    [Container: joplin-cli]
                    Joplin CLI as daemon
                    Syncs every 5 min
                            │
                            ▼
                    [Existing: joplin-server]
                    Joplin Server (Sync Backend)
```

**Why two containers?** The Joplin Data API (port 41184) only exists in the Joplin CLI/Desktop client, NOT in Joplin Server (which is only the sync backend). The `joplin-cli` container runs as a daemon, syncs with Joplin Server, and exposes the Data API on the internal Docker network.

## Available Tools

| Tool | Read-Only | Description |
|------|-----------|-------------|
| `search_notes` | yes | Full-text search across notes |
| `get_note` | yes | Retrieve a single note with body |
| `list_notebooks` | yes | List all notebooks/folders |
| `list_notes_in_notebook` | yes | List notes in a specific notebook |
| `create_note` | no | Create a new note |
| `update_note` | no | Update an existing note |
| `delete_note` | no (destructive) | Delete a note |
| `get_tags` | yes | List all tags |
| `get_notes_by_tag` | yes | Get notes by tag |
| `import_markdown` | no | Import a markdown file as note |

## Quick Start (Docker)

### 1. Configure

```bash
cp .env.example .env
# Edit .env with your values:
#   JOPLIN_SERVER_URL, JOPLIN_SERVER_USER, JOPLIN_SERVER_PASSWORD
#   JOPLIN_TOKEN (generate with: openssl rand -hex 32)
```

### 2. Network Setup

If your Joplin Server runs in an existing Docker network, update `docker-compose.yml`:

```yaml
networks:
  joplin-mcp-net:
    external: true
    name: <your-existing-docker-network>
```

Find the network name with:
```bash
docker network ls
docker inspect <joplin-server-container>
```

### 3. Build & Run

```bash
docker compose build
docker compose up -d
```

### 4. Verify

```bash
# Check container health
docker compose ps

# Test MCP endpoint
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"method":"tools/list","params":{},"id":1,"jsonrpc":"2.0"}'
```

### 5. Connect Claude.ai

In Claude.ai: **Settings → Integrations → Add MCP Server**

URL: `https://joplin-mcp.yourdomain.de/mcp`

## Configuration

All configuration is done via environment variables (see `.env.example`):

| Variable | Description | Default |
|----------|-------------|---------|
| `JOPLIN_SERVER_URL` | URL of your Joplin Server | `http://joplin-server:22300` |
| `JOPLIN_SERVER_USER` | Joplin Server login email | — |
| `JOPLIN_SERVER_PASSWORD` | Joplin Server login password | — |
| `JOPLIN_TOKEN` | API token for the Data API | — |
| `SYNC_INTERVAL` | Sync interval in seconds | `300` |
| `MCP_PORT` | Host port for MCP server | `8000` |
| `JOPLIN_NOTEBOOK_FILTER` | Comma-separated notebook names to restrict access | (empty = all) |

## Local Development (without Docker)

```bash
# Install dependencies
uv venv && source .venv/bin/activate
uv pip install -e .

# Run with MCP Inspector (stdio transport)
JOPLIN_TOKEN=your_token MCP_TRANSPORT=stdio mcp dev src/mcp/joplin_mcp.py
```

### Claude Desktop Setup

Add to your Claude Desktop MCP config:

```json
{
  "mcpServers": {
    "joplin": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/joplin-mcp-server",
        "run", "src/mcp/joplin_mcp.py"
      ]
    }
  }
}
```

## License

[MIT License](LICENSE) — Original work by [David Weigend](https://github.com/dweigend)
