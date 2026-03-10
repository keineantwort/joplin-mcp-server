"""Joplin MCP Server implementation.

Supports both stdio (local/Claude Desktop) and streamable-http (remote/Claude.ai) transport.
Configurable via environment variables.
"""

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


# --- MCP Tools ---


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
        return {"status": "success", "note": _note_to_dict(note), "imported_from": str(path)}
    except Exception as e:
        logger.error("Error importing markdown: %s", e)
        return {"error": str(e)}


if __name__ == "__main__":
    logger.info("Starting Joplin MCP Server (transport=%s)", MCP_TRANSPORT)
    mcp.run(transport=MCP_TRANSPORT)
