"""Joplin MCP Server implementation.

Supports both stdio (local/Claude Desktop) and streamable-http (remote/Claude.ai) transport.
Configurable via environment variables.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

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
    """Run SSE transport with OAuth 2.1 and Bearer token authentication."""
    import hashlib
    import secrets
    import time
    import urllib.parse

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
    from starlette.requests import Request
    from mcp.server.sse import SseServerTransport

    auth_token = os.environ.get("MCP_AUTH_TOKEN", "")
    oauth_client_id = os.environ.get("OAUTH_CLIENT_ID", "")
    oauth_client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "")
    oauth_issuer_url = os.environ.get("OAUTH_ISSUER_URL", "").rstrip("/")

    # In-memory stores for OAuth state
    # auth_codes: {code: {client_id, redirect_uri, code_challenge, expires_at, used}}
    auth_codes: dict[str, dict] = {}
    # access_tokens: {token: {client_id, expires_at}}
    access_tokens: dict[str, dict] = {}

    ALLOWED_REDIRECT_URIS = {
        "https://claude.ai/api/mcp/auth_callback",
    }
    AUTH_CODE_TTL = 60  # seconds
    ACCESS_TOKEN_TTL = 3600  # 1 hour
    REFRESH_TOKEN_TTL = 86400 * 30  # 30 days
    # refresh_tokens: {token: {client_id, expires_at}}
    refresh_tokens: dict[str, dict] = {}

    def _cleanup_expired():
        """Remove expired auth codes and tokens."""
        now = time.time()
        for store in (auth_codes, access_tokens, refresh_tokens):
            expired = [k for k, v in store.items() if v.get("expires_at", 0) < now]
            for k in expired:
                del store[k]

    def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
        """Verify PKCE S256 code challenge."""
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        import base64
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return secrets.compare_digest(computed, code_challenge)

    def _check_bearer(request: Request) -> bool:
        """Check if request has a valid Bearer token (OAuth or static)."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]
        # Check OAuth access tokens
        if token in access_tokens:
            info = access_tokens[token]
            if info["expires_at"] > time.time():
                return True
            del access_tokens[token]
            return False
        # Fall back to static MCP_AUTH_TOKEN
        if auth_token and secrets.compare_digest(token, auth_token):
            return True
        return False

    sse = SseServerTransport("/messages/")

    # --- OAuth Endpoints ---

    async def oauth_metadata(request: Request):
        """RFC 8414 — OAuth Authorization Server Metadata."""
        base = oauth_issuer_url or str(request.base_url).rstrip("/")
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        })

    async def oauth_authorize(request: Request):
        """Authorization endpoint — shows approve page or auto-approves."""
        _cleanup_expired()
        params = request.query_params
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        response_type = params.get("response_type", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        state = params.get("state", "")

        # Validate
        if response_type != "code":
            return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        if oauth_client_id and not secrets.compare_digest(client_id, oauth_client_id):
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if redirect_uri not in ALLOWED_REDIRECT_URIS:
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        if code_challenge_method != "S256":
            return JSONResponse({"error": "invalid_code_challenge_method", "detail": "S256 required"}, status_code=400)
        if not code_challenge:
            return JSONResponse({"error": "missing_code_challenge"}, status_code=400)

        # Auto-approve: generate auth code and redirect immediately
        code = secrets.token_urlsafe(48)
        auth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "expires_at": time.time() + AUTH_CODE_TTL,
            "used": False,
        }

        redirect_params = {"code": code}
        if state:
            redirect_params["state"] = state
        redirect_url = f"{redirect_uri}?{urllib.parse.urlencode(redirect_params)}"
        logger.info("OAuth authorize: issued auth code for client_id=%s", client_id)
        return RedirectResponse(url=redirect_url, status_code=302)

    async def oauth_token(request: Request):
        """Token endpoint — exchanges auth code or refresh token for access token."""
        _cleanup_expired()
        form = await request.form()
        grant_type = form.get("grant_type", "")
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")

        # Validate client credentials
        if oauth_client_id and not secrets.compare_digest(client_id, oauth_client_id):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if oauth_client_secret and not secrets.compare_digest(client_secret, oauth_client_secret):
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        if grant_type == "authorization_code":
            code = form.get("code", "")
            code_verifier = form.get("code_verifier", "")
            redirect_uri = form.get("redirect_uri", "")

            if code not in auth_codes:
                return JSONResponse({"error": "invalid_grant", "error_description": "Unknown or expired code"}, status_code=400)

            code_info = auth_codes[code]
            if code_info["used"]:
                del auth_codes[code]
                return JSONResponse({"error": "invalid_grant", "error_description": "Code already used"}, status_code=400)
            if code_info["expires_at"] < time.time():
                del auth_codes[code]
                return JSONResponse({"error": "invalid_grant", "error_description": "Code expired"}, status_code=400)
            if redirect_uri and code_info["redirect_uri"] != redirect_uri:
                return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

            # PKCE verification
            if not code_verifier:
                return JSONResponse({"error": "invalid_request", "error_description": "code_verifier required"}, status_code=400)
            if not _verify_pkce(code_verifier, code_info["code_challenge"]):
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

            # Mark code as used
            code_info["used"] = True

            # Issue tokens
            access_token = secrets.token_urlsafe(48)
            refresh_token = secrets.token_urlsafe(48)
            access_tokens[access_token] = {
                "client_id": client_id,
                "expires_at": time.time() + ACCESS_TOKEN_TTL,
            }
            refresh_tokens[refresh_token] = {
                "client_id": client_id,
                "expires_at": time.time() + REFRESH_TOKEN_TTL,
            }
            logger.info("OAuth token: issued access_token for client_id=%s", client_id)
            return JSONResponse({
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": refresh_token,
            })

        elif grant_type == "refresh_token":
            rt = form.get("refresh_token", "")
            if rt not in refresh_tokens:
                return JSONResponse({"error": "invalid_grant", "error_description": "Unknown or expired refresh token"}, status_code=400)
            rt_info = refresh_tokens[rt]
            if rt_info["expires_at"] < time.time():
                del refresh_tokens[rt]
                return JSONResponse({"error": "invalid_grant", "error_description": "Refresh token expired"}, status_code=400)

            # Rotate: delete old refresh token, issue new tokens
            del refresh_tokens[rt]
            access_token = secrets.token_urlsafe(48)
            new_refresh_token = secrets.token_urlsafe(48)
            access_tokens[access_token] = {
                "client_id": client_id,
                "expires_at": time.time() + ACCESS_TOKEN_TTL,
            }
            refresh_tokens[new_refresh_token] = {
                "client_id": client_id,
                "expires_at": time.time() + REFRESH_TOKEN_TTL,
            }
            logger.info("OAuth token: refreshed access_token for client_id=%s", client_id)
            return JSONResponse({
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": new_refresh_token,
            })

        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def oauth_register(request: Request):
        """Dynamic Client Registration — not supported, return 501."""
        return JSONResponse(
            {"error": "registration_not_supported", "error_description": "Use pre-configured client_id and client_secret"},
            status_code=501,
        )

    # --- MCP SSE Endpoints ---

    async def handle_sse(request):
        if not _check_bearer(request):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
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
        if not _check_bearer(request):
            response = JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await sse.handle_post_message(scope, receive, send)

    starlette_app = Starlette(
        debug=False,
        routes=[
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
