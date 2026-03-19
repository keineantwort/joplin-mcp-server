"""Joplin MCP Server implementation.

Supports both stdio (local/Claude Desktop) and SSE (remote/Claude.ai) transport.
Authentication and authorization via Authentik (OAuth 2.1 + Token Introspection).
"""

import asyncio
import contextvars
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# Add the src directory to the Python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.joplin.joplin_api import JoplinAPI, JoplinFolder
from src.joplin.joplin_utils import get_token_from_env, MarkdownContent

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration from environment
JOPLIN_HOST = os.environ.get("JOPLIN_HOST", "localhost")
JOPLIN_PORT = os.environ.get("JOPLIN_PORT", "41184")
JOPLIN_BASE_URL = f"http://{JOPLIN_HOST}:{JOPLIN_PORT}"
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
JOPLIN_SYNC_PORT = int(os.environ.get("JOPLIN_SYNC_PORT", "41186"))
JOPLIN_SYNC_PORT_BLOCKING = int(os.environ.get("JOPLIN_SYNC_PORT_BLOCKING", "41187"))
NOTEBOOK_FILTER = [
    n.strip()
    for n in os.environ.get("JOPLIN_NOTEBOOK_FILTER", "").split(",")
    if n.strip()
]

# Public URL of this MCP server (used in OAuth metadata)
MCP_PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")

# Authentik configuration
AUTHENTIK_URL = os.environ.get("AUTHENTIK_URL", "")
AUTHENTIK_SLUG = os.environ.get("AUTHENTIK_SLUG", "joplin-mcp")
AUTHENTIK_CLIENT_ID = os.environ.get("AUTHENTIK_CLIENT_ID", "")
AUTHENTIK_CLIENT_SECRET = os.environ.get("AUTHENTIK_CLIENT_SECRET", "")

# Derived Authentik endpoints
AUTHENTIK_INTROSPECT_URL = f"{AUTHENTIK_URL}/application/o/introspect/"
AUTHENTIK_AUTHORIZE_URL = f"{AUTHENTIK_URL}/application/o/authorize/"
AUTHENTIK_TOKEN_URL = f"{AUTHENTIK_URL}/application/o/token/"
AUTHENTIK_ISSUER_URL = f"{AUTHENTIK_URL}/application/o/{AUTHENTIK_SLUG}/"

# --- Token Introspection ---

_token_cache: dict[str, tuple[set[str], float]] = {}
INTROSPECT_CACHE_TTL = 300  # 5 minutes

# Scopes for the current SSE session (set at connection time)
_current_scopes: contextvars.ContextVar[set[str]] = contextvars.ContextVar(
    "current_scopes", default=set()
)


async def _introspect_token(token: str) -> set[str]:
    """Introspect token via Authentik. Returns granted scopes. Cached for 5 min."""
    now = time.time()
    cached = _token_cache.get(token)
    if cached and cached[1] > now:
        return cached[0]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            AUTHENTIK_INTROSPECT_URL,
            data={"token": token},
            auth=(AUTHENTIK_CLIENT_ID, AUTHENTIK_CLIENT_SECRET),
        )
    if resp.status_code != 200 or not resp.content:
        logger.error("Introspection failed: HTTP %s, body=%r", resp.status_code, resp.text[:200] if resp.text else "")
        raise PermissionError("Token introspection failed")
    data = resp.json()
    if not data.get("active"):
        _token_cache.pop(token, None)
        raise PermissionError("Token invalid or expired")

    scopes = set(data.get("scope", "").split())
    _token_cache[token] = (scopes, now + INTROSPECT_CACHE_TTL)
    return scopes


def _require_scope(scope: str) -> None:
    """Check if the current session has the required scope. Raises PermissionError."""
    scopes = _current_scopes.get()
    if scope not in scopes:
        raise PermissionError(f"403 Forbidden – Scope '{scope}' not granted")

# Initialize FastMCP server
mcp = FastMCP("joplin")

# Initialize Joplin API client
try:
    api = JoplinAPI(token=get_token_from_env(), base_url=JOPLIN_BASE_URL)
    logger.info("Joplin API client initialized (%s)", JOPLIN_BASE_URL)
except Exception as e:
    logger.error("Failed to initialize Joplin API client: %s", e)
    api = None

# --- Notebook filter helpers ---

_allowed_folder_ids: set[str] | None = None


def _get_allowed_folder_ids() -> set[str] | None:
    """Return set of allowed folder IDs based on JOPLIN_NOTEBOOK_FILTER, or None if unfiltered."""
    global _allowed_folder_ids
    if not NOTEBOOK_FILTER:
        return None
    if _allowed_folder_ids is not None:
        return _allowed_folder_ids
    if not api:
        return None
    try:
        folders = api.get_folders()
        _allowed_folder_ids = {
            f.id for f in folders if f.title in NOTEBOOK_FILTER
        }
        logger.info(
            "Notebook filter active: %s -> %d folder(s)",
            NOTEBOOK_FILTER,
            len(_allowed_folder_ids),
        )
        return _allowed_folder_ids
    except Exception as e:
        logger.error("Failed to resolve notebook filter: %s", e)
        return None


def _folder_allowed(folder_id: str | None) -> bool:
    """Check if a folder ID is in the allowed set (or filter is disabled)."""
    allowed = _get_allowed_folder_ids()
    if allowed is None:
        return True
    return folder_id in allowed


# --- Helper to serialize a note ---

def _note_to_dict(note) -> dict:
    return {
        "id": note.id,
        "title": note.title,
        "body": note.body,
        "created_time": note.created_time.isoformat() if note.created_time else None,
        "updated_time": note.updated_time.isoformat() if note.updated_time else None,
        "is_todo": note.is_todo,
    }


def _trigger_sync_background() -> None:
    """Trigger a Joplin sync in the background after write operations."""
    if api:
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, api.trigger_sync, JOPLIN_SYNC_PORT)
        except Exception as e:
            logger.debug("Could not trigger sync: %s", e)


# --- MCP Tools ---


@mcp.tool()
async def sync_notes() -> dict:
    """Synchronize notes with the Joplin Server.

    Triggers a full sync and waits for it to complete. Use this when you
    need up-to-date data, e.g. after the user has manually edited notes
    on another device.
    """
    _require_scope("joplin:sync_notes")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, api.sync_and_wait, JOPLIN_SYNC_PORT_BLOCKING
        )
        return result
    except Exception as e:
        logger.error("Error syncing notes: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def search_notes(query: str, limit: int = 100) -> dict:
    """Search for notes in Joplin using full-text search.

    Args:
        query: Search query string
        limit: Maximum number of results (default: 100)
    """
    _require_scope("joplin:search_notes")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        results = api.search_notes(query=query, limit=limit)
        notes = [_note_to_dict(n) for n in results.items if _folder_allowed(n.parent_id)]
        return {"status": "success", "total": len(notes), "notes": notes}
    except Exception as e:
        logger.error("Error searching notes: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def get_note(note_id: str) -> dict:
    """Retrieve a specific note by its ID, including the full body.

    Args:
        note_id: ID of the note to retrieve
    """
    _require_scope("joplin:get_note")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        note = api.get_note(note_id)
        return {"status": "success", "note": _note_to_dict(note)}
    except Exception as e:
        logger.error("Error getting note: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def list_notebooks() -> dict:
    """List all notebooks/folders available in Joplin."""
    _require_scope("joplin:list_notebooks")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        folders = api.get_folders()
        allowed = _get_allowed_folder_ids()
        result = [
            {"id": f.id, "title": f.title, "parent_id": f.parent_id, "note_count": f.note_count}
            for f in folders
            if allowed is None or f.id in allowed
        ]
        return {"status": "success", "total": len(result), "notebooks": result}
    except Exception as e:
        logger.error("Error listing notebooks: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def list_notes_in_notebook(notebook_id: str, limit: int = 100) -> dict:
    """List all notes in a specific notebook/folder.

    Args:
        notebook_id: ID of the notebook/folder
        limit: Maximum number of results (default: 100)
    """
    _require_scope("joplin:list_notes")
    if not api:
        return {"error": "Joplin API client not initialized"}
    if not _folder_allowed(notebook_id):
        return {"error": "Notebook not in allowed filter"}
    try:
        results = api.get_notes_in_folder(notebook_id, limit=limit)
        notes = [_note_to_dict(n) for n in results.items]
        return {"status": "success", "total": len(notes), "has_more": results.has_more, "notes": notes}
    except Exception as e:
        logger.error("Error listing notes in notebook: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def create_note(title: str, body: str | None = None, parent_id: str | None = None, is_todo: bool = False) -> dict:
    """Create a new note in Joplin.

    Args:
        title: Note title
        body: Note content in Markdown (optional)
        parent_id: ID of parent folder (optional)
        is_todo: Whether this is a todo item (default: false)
    """
    _require_scope("joplin:create_note")
    if not api:
        return {"error": "Joplin API client not initialized"}
    if parent_id and not _folder_allowed(parent_id):
        return {"error": "Target notebook not in allowed filter"}
    try:
        note = api.create_note(title=title, body=body, parent_id=parent_id, is_todo=is_todo)
        _trigger_sync_background()
        return {"status": "success", "note": _note_to_dict(note)}
    except Exception as e:
        logger.error("Error creating note: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def update_note(
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    parent_id: str | None = None,
    is_todo: bool | None = None,
) -> dict:
    """Update an existing note in Joplin.

    Args:
        note_id: ID of note to update
        title: New title (optional)
        body: New content in Markdown (optional)
        parent_id: New parent folder ID (optional)
        is_todo: New todo status (optional)
    """
    _require_scope("joplin:update_note")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        note = api.update_note(note_id=note_id, title=title, body=body, parent_id=parent_id, is_todo=is_todo)
        _trigger_sync_background()
        return {"status": "success", "note": _note_to_dict(note)}
    except Exception as e:
        logger.error("Error updating note: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def delete_note(note_id: str, permanent: bool = False) -> dict:
    """Delete a note from Joplin.

    Args:
        note_id: ID of note to delete
        permanent: If true, permanently delete instead of moving to trash
    """
    _require_scope("joplin:delete_note")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        api.delete_note(note_id, permanent=permanent)
        _trigger_sync_background()
        return {"status": "success", "message": f"Note {note_id} {'permanently ' if permanent else ''}deleted"}
    except Exception as e:
        logger.error("Error deleting note: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def get_tags() -> dict:
    """List all tags in Joplin."""
    _require_scope("joplin:get_tags")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        tags = api.get_tags()
        return {
            "status": "success",
            "total": len(tags),
            "tags": [{"id": t.id, "title": t.title} for t in tags],
        }
    except Exception as e:
        logger.error("Error getting tags: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def get_notes_by_tag(tag_id: str, limit: int = 100) -> dict:
    """Get all notes that have a specific tag.

    Args:
        tag_id: ID of the tag
        limit: Maximum number of results (default: 100)
    """
    _require_scope("joplin:get_notes_by_tag")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        results = api.get_notes_by_tag(tag_id, limit=limit)
        notes = [_note_to_dict(n) for n in results.items if _folder_allowed(n.parent_id)]
        return {"status": "success", "total": len(notes), "has_more": results.has_more, "notes": notes}
    except Exception as e:
        logger.error("Error getting notes by tag: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def import_markdown(file_path: str) -> dict:
    """Import a markdown file as a new note.

    Args:
        file_path: Path to the markdown file
    """
    _require_scope("joplin:import_markdown")
    if not api:
        return {"error": "Joplin API client not initialized"}
    try:
        path = Path(file_path)
        md_content = MarkdownContent.from_file(path)
        note = api.create_note(title=md_content.title, body=md_content.content)
        _trigger_sync_background()
        return {"status": "success", "note": _note_to_dict(note), "imported_from": str(path)}
    except Exception as e:
        logger.error("Error importing markdown: %s", e)
        return {"error": str(e)}


async def run_sse_with_auth() -> None:
    """Run SSE transport with Authentik OAuth 2.1 authorization.

    OAuth metadata points to Authentik endpoints. Token validation and
    scope checking is done via Authentik Token Introspection.
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import JSONResponse
    from starlette.requests import Request
    from mcp.server.sse import SseServerTransport

    def _extract_bearer(request: Request) -> str | None:
        """Extract Bearer token from Authorization header."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        return auth_header[7:]

    sse = SseServerTransport("/messages/")

    # --- OAuth Metadata (points to Authentik) ---

    async def oauth_metadata(request: Request):
        """RFC 8414 — OAuth Authorization Server Metadata pointing to Authentik."""
        return JSONResponse({
            "issuer": MCP_PUBLIC_URL,
            "authorization_endpoint": f"{MCP_PUBLIC_URL}/oauth/authorize",
            "token_endpoint": f"{MCP_PUBLIC_URL}/oauth/token",
            "registration_endpoint": f"{MCP_PUBLIC_URL}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        })

    async def oauth_protected_resource(request: Request):
        """RFC 9728 — OAuth Protected Resource Metadata."""
        return JSONResponse({
            "resource": MCP_PUBLIC_URL,
            "authorization_servers": [MCP_PUBLIC_URL],
            "scopes_supported": [
                "joplin:get_note", "joplin:search_notes", "joplin:list_notebooks",
                "joplin:list_notes", "joplin:get_tags", "joplin:get_notes_by_tag",
                "joplin:create_note", "joplin:update_note", "joplin:delete_note",
                "joplin:import_markdown", "joplin:sync_notes",
            ],
            "bearer_methods_supported": ["header"],
        })

    async def oauth_register(request: Request):
        """Dynamic Client Registration — returns pre-configured Authentik credentials.

        Claude Code needs client_id/secret to start the OAuth flow.
        Returns the Authentik provider credentials so the client can
        proceed with the authorization code flow against Authentik.
        """
        return JSONResponse({
            "client_id": AUTHENTIK_CLIENT_ID,
            "client_secret": AUTHENTIK_CLIENT_SECRET,
            "client_name": "Joplin MCP",
            "redirect_uris": [
                "https://claude.ai/api/mcp/auth_callback",
                "http://localhost:8080/callback",
            ],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }, status_code=201)

    # --- OAuth Proxy Endpoints (forward to Authentik) ---

    async def oauth_authorize(request: Request):
        """Redirect authorization requests to Authentik.

        Ensures 'offline_access' is included in the scope parameter so that
        Authentik issues a refresh token alongside the access token.
        """
        from starlette.responses import RedirectResponse
        from urllib.parse import urlencode, parse_qs
        params = parse_qs(str(request.url.query), keep_blank_values=True)
        # Inject offline_access into scope if not already present
        scope_values = params.get("scope", [""])[0].split()
        if "offline_access" not in scope_values:
            scope_values.append("offline_access")
            params["scope"] = [" ".join(scope_values)]
        # Rebuild query string (parse_qs returns lists, flatten for urlencode)
        flat_params = {k: v[0] for k, v in params.items()}
        redirect_url = f"{AUTHENTIK_AUTHORIZE_URL}?{urlencode(flat_params)}"
        return RedirectResponse(url=redirect_url, status_code=302)

    async def oauth_token(request: Request):
        """Proxy token requests to Authentik."""
        body = await request.body()
        logger.info("Token request: %s", body.decode("utf-8", errors="replace"))
        headers = {
            "content-type": request.headers.get("content-type", "application/x-www-form-urlencoded"),
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(AUTHENTIK_TOKEN_URL, content=body, headers=headers)
        token_data = resp.json()
        logger.info("Token response: status=%s, has_refresh_token=%s, expires_in=%s",
                     resp.status_code, "refresh_token" in token_data, token_data.get("expires_in"))
        return JSONResponse(token_data, status_code=resp.status_code)

    # --- MCP SSE Endpoints ---

    async def handle_sse(request):
        token = _extract_bearer(request)
        if not token:
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            scopes = await _introspect_token(token)
        except PermissionError:
            return JSONResponse(
                {"error": "Unauthorized – token invalid or expired"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Store scopes in contextvar so tool handlers can check them
        _current_scopes.set(scopes)
        logger.info("SSE connected, granted scopes: %s", scopes)

        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp._mcp_server.run(
                streams[0],
                streams[1],
                mcp._mcp_server.create_initialization_options(),
            )

    async def handle_messages(scope, receive, send):
        request = Request(scope, receive, send)
        token = _extract_bearer(request)
        if not token:
            response = JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        try:
            await _introspect_token(token)  # validates (uses cache)
        except PermissionError:
            response = JSONResponse(
                {"error": "Unauthorized – token invalid or expired"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await sse.handle_post_message(scope, receive, send)

    starlette_app = Starlette(
        debug=False,
        routes=[
            Route("/.well-known/oauth-protected-resource", endpoint=oauth_protected_resource),
            Route("/.well-known/oauth-authorization-server", endpoint=oauth_metadata),
            Route("/oauth/authorize", endpoint=oauth_authorize),
            Route("/oauth/token", endpoint=oauth_token, methods=["POST"]),
            Route("/oauth/register", endpoint=oauth_register, methods=["POST"]),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=handle_messages),
        ],
    )

    config = uvicorn.Config(starlette_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    logger.info("Starting Joplin MCP Server (transport=%s)", MCP_TRANSPORT)
    if MCP_TRANSPORT == "sse":
        import anyio
        anyio.run(run_sse_with_auth)
    else:
        mcp.run(transport=MCP_TRANSPORT)
