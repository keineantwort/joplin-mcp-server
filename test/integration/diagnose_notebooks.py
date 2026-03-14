#!/usr/bin/env python3
"""Diagnose notebook visibility in the Joplin MCP server.

Connects to the Joplin Data API and shows:
  - The full notebook tree as returned by the API
  - Which notebooks the JOPLIN_NOTEBOOK_FILTER allows/blocks
  - Sub-notebooks that are excluded because only their parent is listed
  - Note counts (total, visible, hidden)

Configuration (via environment or .env file):
  JOPLIN_TOKEN             API token (required)
  JOPLIN_HOST              Joplin API host (default: localhost)
  JOPLIN_PORT              Joplin API port (default: 41184)
  JOPLIN_NOTEBOOK_FILTER   Comma-separated notebook names to allow (optional)

Usage:
  # Using environment variables
  JOPLIN_TOKEN=abc123 python diagnose_notebooks.py

  # Using .env file (auto-loaded)
  python diagnose_notebooks.py

  # Override host/port
  JOPLIN_HOST=192.168.1.10 python diagnose_notebooks.py

  # Simulate a filter without changing .env
  python diagnose_notebooks.py --filter "Work,Personal"
"""

import argparse
import os
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.joplin.joplin_api import JoplinAPI, JoplinFolder
from src.joplin.joplin_utils import get_token_from_env


def build_tree(
    folders: list[JoplinFolder], parent_id: str | None = None, depth: int = 0
):
    """Yield (depth, folder) tuples in alphabetical tree order."""
    children = [f for f in folders if (f.parent_id or None) == parent_id]
    children.sort(key=lambda f: f.title.lower())
    for folder in children:
        yield depth, folder
        yield from build_tree(folders, parent_id=folder.id, depth=depth + 1)


def print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def print_tree(folders: list[JoplinFolder], highlight_ids: set[str] | None = None) -> None:
    """Print the notebook tree. Optionally prefix allowed/blocked markers."""
    for depth, f in build_tree(folders):
        indent = "  " * depth
        notes = f" ({f.note_count} notes)" if f.note_count else ""
        if highlight_ids is not None:
            marker = "+" if f.id in highlight_ids else "-"
            print(f"  {marker} {indent}{f.title}{notes}  [{f.id[:8]}...]")
        else:
            print(f"  {indent}{f.title}{notes}  [{f.id[:8]}...]")


def analyze_filter(folders: list[JoplinFolder], notebook_filter: list[str]) -> None:
    """Analyze which notebooks the filter allows and flag potential issues."""
    allowed_ids = {f.id for f in folders if f.title in notebook_filter}
    matched_names = {f.title for f in folders if f.id in allowed_ids}
    unmatched = set(notebook_filter) - matched_names

    print_section("NOTEBOOK FILTER ANALYSIS")
    print(f"  Filter entries: {notebook_filter}")
    print(f"  Matched:        {len(allowed_ids)} folder(s)")

    # Warn about filter entries that don't match any folder
    if unmatched:
        print()
        print("  WARNING — these filter entries matched NO folder:")
        for name in sorted(unmatched):
            print(f"    ? {name!r}")

    # Show allowed notebooks
    print()
    print("  Allowed notebooks:")
    for f in sorted(
        (f for f in folders if f.id in allowed_ids), key=lambda f: f.title.lower()
    ):
        print(f"    + {f.title}  [{f.id[:8]}...]")

    # Detect sub-notebooks excluded because only parent is in the filter
    missing_children = [
        f for f in folders if f.parent_id in allowed_ids and f.id not in allowed_ids
    ]
    if missing_children:
        print()
        print("  WARNING — sub-notebooks excluded (parent is allowed, child is NOT):")
        for f in sorted(missing_children, key=lambda f: f.title.lower()):
            parent = next((p for p in folders if p.id == f.parent_id), None)
            parent_name = parent.title if parent else "?"
            print(f"    ! {f.title}  (child of {parent_name!r})  [{f.id[:8]}...]")

    # Show filtered tree with +/- markers
    print_section("FILTERED TREE  (+ = allowed, - = blocked)")
    print_tree(folders, highlight_ids=allowed_ids)

    # Note counts
    total_notes = sum(f.note_count for f in folders)
    visible_notes = sum(f.note_count for f in folders if f.id in allowed_ids)
    print_section("NOTE COUNTS")
    print(f"  Total notes:   {total_notes}")
    print(f"  Visible:       {visible_notes}")
    print(f"  Hidden:        {total_notes - visible_notes}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose notebook visibility in the Joplin MCP server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables (also read from .env):\n"
            "  JOPLIN_TOKEN              API token (required)\n"
            "  JOPLIN_HOST               API host (default: localhost)\n"
            "  JOPLIN_PORT               API port (default: 41184)\n"
            "  JOPLIN_NOTEBOOK_FILTER    Comma-separated notebook names\n"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Joplin API host (overrides JOPLIN_HOST env var)",
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Joplin API port (overrides JOPLIN_PORT env var)",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Comma-separated notebook names to simulate (overrides JOPLIN_NOTEBOOK_FILTER)",
    )
    args = parser.parse_args()

    host = args.host or os.environ.get("JOPLIN_HOST", "localhost")
    port = args.port or os.environ.get("JOPLIN_PORT", "41184")
    base_url = f"http://{host}:{port}"

    filter_raw = args.filter if args.filter is not None else os.environ.get("JOPLIN_NOTEBOOK_FILTER", "")
    notebook_filter = [n.strip() for n in filter_raw.split(",") if n.strip()]

    print(f"Joplin API:             {base_url}")
    print(f"JOPLIN_NOTEBOOK_FILTER: {filter_raw!r}" if filter_raw else "JOPLIN_NOTEBOOK_FILTER: (not set)")

    # --- Connect ---
    try:
        token = get_token_from_env()
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    api = JoplinAPI(token=token, base_url=base_url)

    # --- Fetch folders ---
    try:
        folders = api.get_folders()
    except Exception as e:
        print(f"\nERROR: Could not fetch folders from {base_url}: {e}", file=sys.stderr)
        return 1

    print(f"Folders returned:       {len(folders)}")

    # --- Full tree ---
    print_section("FULL NOTEBOOK TREE")
    print_tree(folders)

    # --- Filter analysis ---
    if notebook_filter:
        analyze_filter(folders, notebook_filter)
    else:
        print_section("NOTE COUNTS")
        total_notes = sum(f.note_count for f in folders)
        print(f"  Total notes: {total_notes}")
        print()
        print("  No JOPLIN_NOTEBOOK_FILTER set — all notebooks are accessible.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
