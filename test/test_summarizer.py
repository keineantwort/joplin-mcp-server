"""Tests for the LLM summarizer module."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.utils.summarizer import (
    _apply_scores,
    _fallback_summarize,
    _truncate,
    summarize_notes,
)


# --- Sample data ---

def _make_notes(n: int = 3) -> list[dict]:
    return [
        {
            "id": f"note-{i}",
            "title": f"Note {i}",
            "body": f"This is the body of note {i}. " * 20,
            "created_time": "2026-01-01T00:00:00",
            "updated_time": "2026-01-02T00:00:00",
            "is_todo": False,
        }
        for i in range(n)
    ]


# --- _truncate ---

class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_long_text_truncated(self):
        text = "x" * 300
        result = _truncate(text)
        assert result == "x" * 200 + "..."
        assert len(result) == 203

    def test_empty_string(self):
        assert _truncate("") == ""

    def test_none_returns_empty(self):
        assert _truncate(None) == ""

    def test_custom_length(self):
        result = _truncate("abcdefgh", length=5)
        assert result == "abcde..."

    def test_exact_length_no_truncation(self):
        text = "x" * 200
        assert _truncate(text) == text


# --- _fallback_summarize ---

class TestFallbackSummarize:
    def test_replaces_body_with_summary(self):
        notes = _make_notes(2)
        result = _fallback_summarize(notes)
        assert len(result) == 2
        for note in result:
            assert "body" not in note
            assert "summary" in note
            assert note["id"].startswith("note-")

    def test_preserves_other_fields(self):
        notes = [{"id": "1", "title": "T", "body": "B", "is_todo": True}]
        result = _fallback_summarize(notes)
        assert result[0]["id"] == "1"
        assert result[0]["title"] == "T"
        assert result[0]["is_todo"] is True

    def test_empty_body(self):
        notes = [{"id": "1", "title": "T", "body": ""}]
        result = _fallback_summarize(notes)
        assert result[0]["summary"] == ""

    def test_none_body(self):
        notes = [{"id": "1", "title": "T", "body": None}]
        result = _fallback_summarize(notes)
        assert result[0]["summary"] == ""

    def test_missing_body(self):
        notes = [{"id": "1", "title": "T"}]
        result = _fallback_summarize(notes)
        assert result[0]["summary"] == ""

    def test_does_not_mutate_original(self):
        notes = [{"id": "1", "title": "T", "body": "original"}]
        _fallback_summarize(notes)
        assert notes[0]["body"] == "original"

    def test_empty_list(self):
        assert _fallback_summarize([]) == []


# --- _apply_scores ---

class TestApplyScores:
    def test_applies_summary_without_query(self):
        notes = _make_notes(2)
        scored = [
            {"index": 0, "summary": "Summary 0"},
            {"index": 1, "summary": "Summary 1"},
        ]
        result = _apply_scores(notes, scored, has_query=False)
        assert len(result) == 2
        assert result[0]["summary"] == "Summary 0"
        assert "relevance_score" not in result[0]
        assert "body" not in result[0]

    def test_applies_score_with_query(self):
        notes = _make_notes(3)
        scored = [
            {"index": 2, "score": 9, "summary": "Best match"},
            {"index": 0, "score": 5, "summary": "Okay match"},
        ]
        result = _apply_scores(notes, scored, has_query=True)
        assert len(result) == 2
        assert result[0]["relevance_score"] == 9
        assert result[0]["id"] == "note-2"
        assert result[1]["relevance_score"] == 5
        assert result[1]["id"] == "note-0"

    def test_sorts_by_score_descending(self):
        notes = _make_notes(3)
        scored = [
            {"index": 0, "score": 3, "summary": "Low"},
            {"index": 1, "score": 8, "summary": "High"},
            {"index": 2, "score": 5, "summary": "Mid"},
        ]
        result = _apply_scores(notes, scored, has_query=True)
        scores = [r["relevance_score"] for r in result]
        assert scores == [8, 5, 3]

    def test_skips_invalid_indices(self):
        notes = _make_notes(2)
        scored = [
            {"index": 0, "summary": "Ok"},
            {"index": 99, "summary": "Bad index"},
            {"index": -1, "summary": "Negative"},
            {"summary": "Missing index"},
        ]
        result = _apply_scores(notes, scored, has_query=False)
        assert len(result) == 1
        assert result[0]["id"] == "note-0"

    def test_empty_scored_list(self):
        notes = _make_notes(2)
        result = _apply_scores(notes, [], has_query=True)
        assert result == []

    def test_removes_body_from_output(self):
        notes = _make_notes(1)
        scored = [{"index": 0, "summary": "S"}]
        result = _apply_scores(notes, scored, has_query=False)
        assert "body" not in result[0]


# --- summarize_notes (async, mocked) ---

def _mock_llm_response(content: str, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx.Response with the given LLM content."""
    return httpx.Response(
        status_code=status_code,
        json={
            "choices": [{"message": {"content": content}}],
        },
        request=httpx.Request("POST", "https://fake.api/v1/chat/completions"),
    )


class TestSummarizeNotes:
    @pytest.mark.asyncio
    async def test_fallback_when_no_api_key(self):
        """Without LLM_API_KEY, falls back to truncation."""
        with patch("src.utils.summarizer.LLM_API_KEY", ""):
            notes = _make_notes(2)
            result = await summarize_notes(notes, query="test")
            assert len(result) == 2
            for note in result:
                assert "summary" in note
                assert "body" not in note

    @pytest.mark.asyncio
    async def test_empty_notes_returns_empty(self):
        result = await summarize_notes([], query="anything")
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_summarization_without_query(self):
        llm_response = json.dumps([
            {"index": 0, "summary": "First note summary"},
            {"index": 1, "summary": "Second note summary"},
        ])
        mock_resp = _mock_llm_response(llm_response)

        with patch("src.utils.summarizer.LLM_API_KEY", "test-key"):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
                notes = _make_notes(2)
                result = await summarize_notes(notes)
                assert len(result) == 2
                assert result[0]["summary"] == "First note summary"
                assert result[1]["summary"] == "Second note summary"
                assert "body" not in result[0]
                assert "relevance_score" not in result[0]

    @pytest.mark.asyncio
    async def test_successful_search_with_scoring(self):
        llm_response = json.dumps([
            {"index": 1, "score": 9, "summary": "Very relevant"},
            {"index": 0, "score": 4, "summary": "Somewhat relevant"},
        ])
        mock_resp = _mock_llm_response(llm_response)

        with patch("src.utils.summarizer.LLM_API_KEY", "test-key"):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
                notes = _make_notes(3)
                result = await summarize_notes(notes, query="budget meeting")
                assert len(result) == 2  # note-2 filtered out (not in LLM response)
                assert result[0]["relevance_score"] == 9
                assert result[0]["id"] == "note-1"
                assert result[1]["relevance_score"] == 4

    @pytest.mark.asyncio
    async def test_strips_markdown_code_fences(self):
        llm_response = '```json\n[{"index": 0, "summary": "Fenced"}]\n```'
        mock_resp = _mock_llm_response(llm_response)

        with patch("src.utils.summarizer.LLM_API_KEY", "test-key"):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
                notes = _make_notes(1)
                result = await summarize_notes(notes)
                assert result[0]["summary"] == "Fenced"

    @pytest.mark.asyncio
    async def test_fallback_on_api_error(self):
        mock_resp = _mock_llm_response("", status_code=500)

        with patch("src.utils.summarizer.LLM_API_KEY", "test-key"):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
                notes = _make_notes(2)
                result = await summarize_notes(notes)
                assert len(result) == 2
                for note in result:
                    assert "summary" in note

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self):
        mock_resp = _mock_llm_response("this is not json at all")

        with patch("src.utils.summarizer.LLM_API_KEY", "test-key"):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
                notes = _make_notes(2)
                result = await summarize_notes(notes)
                assert len(result) == 2
                for note in result:
                    assert "summary" in note

    @pytest.mark.asyncio
    async def test_fallback_on_network_exception(self):
        with patch("src.utils.summarizer.LLM_API_KEY", "test-key"):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.ConnectError("connection refused")):
                notes = _make_notes(2)
                result = await summarize_notes(notes)
                assert len(result) == 2
                for note in result:
                    assert "summary" in note

    @pytest.mark.asyncio
    async def test_sends_correct_request(self):
        """Verify the API request format is correct."""
        llm_response = json.dumps([{"index": 0, "summary": "S"}])
        mock_resp = _mock_llm_response(llm_response)
        mock_post = AsyncMock(return_value=mock_resp)

        with patch("src.utils.summarizer.LLM_API_KEY", "my-secret-key"):
            with patch("src.utils.summarizer.LLM_API_URL", "https://api.test.com/v1/chat"):
                with patch("src.utils.summarizer.LLM_MODEL", "test-model"):
                    with patch("httpx.AsyncClient.post", mock_post):
                        await summarize_notes(_make_notes(1), query="test query")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.args[0] == "https://api.test.com/v1/chat"
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer my-secret-key"
        body = call_kwargs.kwargs["json"]
        assert body["model"] == "test-model"
        assert body["temperature"] == 0.1
        assert len(body["messages"]) == 2
        assert "test query" in body["messages"][1]["content"]
