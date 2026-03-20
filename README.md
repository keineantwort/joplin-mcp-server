# Joplin MCP Server — Docker Edition

A Docker-based [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives Claude.ai access to a self-hosted [Joplin](https://joplinapp.org/) note collection via Streamable HTTP transport.

Fork of [dweigend/joplin-mcp-server](https://github.com/dweigend/joplin-mcp-server), extended with remote HTTP transport, OAuth 2.1, E2EE support, and Docker deployment.

## Architecture

```
Claude.ai ──HTTPS──► Reverse Proxy (NPMPlus)
                            │
                            │ :8000 (internal)
                            ▼
                    [Container: joplin-mcp]
                    Python FastMCP Server
                    Streamable HTTP Transport
                    OAuth 2.1 + Bearer Auth
                            │
                            │ :41184 (Data API)
                            │ :41186 (async sync trigger)
                            │ :41187 (blocking sync trigger)
                            ▼
                    [Container: joplin-cli]
                    Joplin CLI as daemon
                    Syncs periodically + on write
                    E2EE decryption
                            │
                            ▼
                    [Existing: joplin-server]
                    Joplin Server (Sync Backend)
```

**Why two containers?** The Joplin Data API (port 41184) only exists in the Joplin CLI/Desktop client, NOT in Joplin Server (which is only the sync backend). The `joplin-cli` container runs as a daemon, syncs with Joplin Server, and exposes the Data API on the internal Docker network.

## Available Tools

| Tool | Type | Description |
| ---- | ---- | ----------- |
| `sync_notes` | sync | Trigger a full sync and wait for completion |
| `search_notes` | read | Full-text search with LLM relevance scoring and summaries |
| `get_note` | read | Retrieve a single note with full body |
| `list_notebooks` | read | List all notebooks/folders |
| `list_notes_in_notebook` | read | List notes in a notebook (with LLM summaries) |
| `get_tags` | read | List all tags |
| `get_notes_by_tag` | read | Get notes by tag (with LLM summaries) |
| `create_note` | write | Create a new note (triggers sync) |
| `update_note` | write | Update an existing note (triggers sync) |
| `delete_note` | write | Delete a note (triggers sync) |
| `import_markdown` | write | Import a markdown file as note (triggers sync) |

Write operations automatically trigger an async background sync so changes reach the Joplin Server within seconds. The `sync_notes` tool can be used to explicitly pull latest changes before reading.

### LLM-Powered Summarization

List and search tools (`search_notes`, `list_notes_in_notebook`, `get_notes_by_tag`) use a cheap LLM to pre-process results before returning them to Claude:

- **Search**: Each note is scored for relevance (0–10) and summarized. Only relevant results are returned, sorted by score.
- **List/Tag**: Each note body is replaced with a 1–2 sentence summary.
- **get_note**: Always returns the full body (no summarization).

This reduces token usage dramatically (e.g. 100 full note bodies → 20 scored summaries) and improves search quality through semantic relevance scoring beyond simple keyword matching.

The LLM integration is **optional** — without an API key, it falls back to simple text truncation. Any OpenAI-compatible API works (DeepInfra, OpenAI, Ollama, vLLM, etc.).

## Quick Start (Docker)

### 1. Configure

```bash
cp .env.example .env
# Edit .env with your values
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

URL: `https://your-mcp-server.example.com/sse`

The OAuth 2.1 flow handles authentication automatically via Authentik.

### Authentik Setup

1. Create an **OAuth2/OpenID Provider** in Authentik for `joplin-mcp`
2. Add a **Scope Mapping** for `offline_access` (expression: `return {}`) — this enables refresh tokens
3. Assign the `offline_access` scope to the provider under **Advanced protocol settings → Scopes**
4. Set **Refresh Token validity** to a reasonable value (e.g. `days=30`)
5. Copy Client ID and Secret to your `.env`

The MCP server automatically injects `offline_access` into the authorization request so that Authentik issues a refresh token alongside the access token. Clients (Claude Code, claude.ai) use the refresh token to transparently renew expired access tokens without re-authentication.

## Configuration

All configuration is done via environment variables (see `.env.example`):

| Variable | Description | Default |
| -------- | ----------- | ------- |
| `JOPLIN_SERVER_URL` | URL of your Joplin Server | `http://joplin-server:22300` |
| `JOPLIN_SERVER_USER` | Joplin Server login email | — |
| `JOPLIN_SERVER_PASSWORD` | Joplin Server login password | — |
| `JOPLIN_TOKEN` | API token for the Data API | — |
| `JOPLIN_ENCRYPTION_PASSWORD` | E2EE master key password (leave empty if not using E2EE) | (empty) |
| `SYNC_INTERVAL` | Periodic sync interval in seconds | `300` |
| `MCP_PORT` | Host port for MCP server | `8000` |
| `JOPLIN_NOTEBOOK_FILTER` | Comma-separated notebook names to restrict access | (empty = all) |
| `MCP_PUBLIC_URL` | Public HTTPS URL of the MCP server (used in OAuth metadata) | — |
| `AUTHENTIK_URL` | Base URL of your Authentik instance | — |
| `AUTHENTIK_SLUG` | Application slug in Authentik | `joplin-mcp` |
| `AUTHENTIK_CLIENT_ID` | OAuth2 Client ID from the Authentik Provider | — |
| `AUTHENTIK_CLIENT_SECRET` | OAuth2 Client Secret from the Authentik Provider | — |
| `LLM_API_URL` | OpenAI-compatible chat completions endpoint | `https://api.deepinfra.com/v1/openai/chat/completions` |
| `LLM_API_KEY` | API key for the LLM provider (empty = disable summarization) | (empty) |
| `LLM_MODEL` | Model identifier | `google/gemma-3-4b-it` |

## Local Development (without Docker)

```bash
# Install dependencies
uv venv && source .venv/bin/activate
uv pip install -e .

# Run with MCP Inspector (stdio transport)
JOPLIN_TOKEN=your_token MCP_TRANSPORT=stdio mcp dev src/mcp/joplin_mcp.py
```

For integration tests against a real Joplin Server, see [test/integration/README.md](test/integration/README.md).

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
