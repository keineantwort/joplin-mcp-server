"""LLM-based note summarization and relevance scoring (OpenAI-compatible API).

Uses a cheap model (e.g. Gemma 3 4B via DeepInfra) to score and summarize
Joplin notes before returning them to Claude, reducing token usage significantly.

Configurable via environment variables:
    LLM_API_URL:   OpenAI-compatible chat completions endpoint
    LLM_API_KEY:   API key (if empty, summarization is disabled)
    LLM_MODEL:     Model identifier
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Configuration — any OpenAI-compatible endpoint works
LLM_API_URL = os.environ.get(
    "LLM_API_URL", "https://api.deepinfra.com/v1/openai/chat/completions"
)
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "google/gemma-3-4b-it")

# Limits
_MAX_BODY_CHARS = 2000  # max chars per note body sent to LLM
_TRUNCATE_FALLBACK = 200  # fallback truncation length when LLM unavailable


def _truncate(text: str, length: int = _TRUNCATE_FALLBACK) -> str:
    """Simple truncation fallback."""
    if not text:
        return ""
    return text[:length] + "..." if len(text) > length else text


async def summarize_notes(notes: list[dict], query: str | None = None) -> list[dict]:
    """Score and summarize a batch of notes using a cheap LLM.

    If a query is provided, the LLM scores relevance (0-10) and sorts by score.
    If no query is provided, each note just gets a 1-2 sentence summary.

    Falls back to simple truncation if no API key is configured or the API fails.

    Args:
        notes: List of note dicts (must have "id", "title", "body")
        query: Optional search query for relevance scoring

    Returns:
        List of note dicts with "body" replaced by "summary" (and "relevance_score"
        if query was provided), sorted by relevance descending.
    """
    if not LLM_API_KEY or not notes:
        return _fallback_summarize(notes)

    # Build the prompt with all notes batched together
    notes_text = ""
    for i, note in enumerate(notes):
        body = (note.get("body") or "")[:_MAX_BODY_CHARS]
        notes_text += f"\n--- NOTE {i} ---\nTitle: {note['title']}\nBody: {body}\n"

    if query:
        system_prompt = (
            "You score notes for relevance to a search query.\n\n"
            "RULES:\n"
            "- Output ONLY a JSON array, no other text\n"
            "- Each element: {\"index\": <int>, \"score\": <0-10>, \"summary\": \"<string>\"}\n"
            "- summary: Describe what the note is about in ONE short sentence (max 20 words). "
            "Do NOT copy the note text. Do NOT include markdown formatting.\n"
            "- score: 0 = completely irrelevant, 10 = perfect match\n"
            "- Only include notes with score >= 3\n"
            "- Order by score descending\n\n"
            "EXAMPLE output:\n"
            '[{"index": 0, "score": 8, "summary": "Meeting notes about Q1 budget decisions and cost planning."}, '
            '{"index": 2, "score": 5, "summary": "General project overview mentioning budget briefly."}]'
        )
        user_prompt = f"Search query: {query}\n\nNotes to evaluate:{notes_text}"
    else:
        system_prompt = (
            "You summarize notes.\n\n"
            "RULES:\n"
            "- Output ONLY a JSON array, no other text\n"
            "- Each element: {\"index\": <int>, \"summary\": \"<string>\"}\n"
            "- summary: Describe what the note is about in ONE short sentence (max 20 words). "
            "Do NOT copy the note text. Do NOT include markdown formatting.\n\n"
            "EXAMPLE output:\n"
            '[{"index": 0, "summary": "Setup guide for deploying a Joplin MCP server with Docker."}, '
            '{"index": 1, "summary": "Personal shopping list for weekend groceries."}]'
        )
        user_prompt = f"Notes to summarize:{notes_text}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                LLM_API_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 4096,
                    "temperature": 0.1,
                },
            )

        if resp.status_code != 200:
            logger.warning("LLM API returned %s, falling back to truncation", resp.status_code)
            return _fallback_summarize(notes)

        content = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        scored = json.loads(content)
        return _apply_scores(notes, scored, has_query=query is not None)

    except Exception as e:
        logger.warning("LLM summarization failed: %s, falling back to truncation", e)
        return _fallback_summarize(notes)


def _apply_scores(notes: list[dict], scored: list[dict], has_query: bool) -> list[dict]:
    """Apply LLM scores/summaries back to the original note dicts."""
    result = []
    for item in scored:
        idx = item.get("index")
        if idx is None or idx < 0 or idx >= len(notes):
            continue
        note = dict(notes[idx])
        note.pop("body", None)
        note["summary"] = item.get("summary", "")
        if has_query:
            note["relevance_score"] = item.get("score", 0)
        result.append(note)

    if has_query:
        result.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return result


def _fallback_summarize(notes: list[dict]) -> list[dict]:
    """Fallback: replace body with truncated version when LLM is unavailable."""
    result = []
    for note in notes:
        note = dict(note)
        body = note.pop("body", "") or ""
        note["summary"] = _truncate(body)
        result.append(note)
    return result
