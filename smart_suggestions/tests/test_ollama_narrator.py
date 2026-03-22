import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from ollama_narrator import OllamaNarrator


def make_candidate(entity_id: str, reason: str = "test reason") -> dict:
    return {
        "entity_id": entity_id,
        "name": entity_id.split(".")[1].replace("_", " ").title(),
        "type": "scene",
        "confidence": "high",
        "reason": reason,
    }


@pytest.mark.asyncio
async def test_narrate_rewrites_reasons():
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [make_candidate("scene.evening", "you usually do this")]

    new_reasons = [{"entity_id": "scene.evening", "reason": "Your living room is ready for Evening Scene."}]

    with patch.object(narrator, "_call_ollama", new=AsyncMock(return_value=json.dumps(new_reasons))):
        result = await narrator.narrate(candidates)

    assert result[0]["entity_id"] == "scene.evening"
    assert result[0]["reason"] == "Your living room is ready for Evening Scene."


@pytest.mark.asyncio
async def test_narrate_falls_back_on_ollama_failure():
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [make_candidate("scene.evening", "original reason")]

    with patch.object(narrator, "_call_ollama", new=AsyncMock(side_effect=Exception("connection refused"))):
        result = await narrator.narrate(candidates)

    assert result[0]["reason"] == "original reason"


@pytest.mark.asyncio
async def test_narrate_falls_back_on_bad_json():
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [make_candidate("scene.evening", "original reason")]

    with patch.object(narrator, "_call_ollama", new=AsyncMock(return_value="not valid json")):
        result = await narrator.narrate(candidates)

    assert result[0]["reason"] == "original reason"


@pytest.mark.asyncio
async def test_narrate_preserves_candidate_count_and_order():
    """Ollama cannot remove or reorder candidates."""
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [
        make_candidate("scene.evening", "reason 1"),
        make_candidate("light.kitchen", "reason 2"),
    ]
    # Ollama returns only one item (fewer than input)
    partial = json.dumps([{"entity_id": "scene.evening", "reason": "Better reason"}])

    with patch.object(narrator, "_call_ollama", new=AsyncMock(return_value=partial)):
        result = await narrator.narrate(candidates)

    assert len(result) == 2
    assert result[0]["entity_id"] == "scene.evening"
    assert result[0]["reason"] == "Better reason"
    assert result[1]["reason"] == "reason 2"  # fallback for missing


@pytest.mark.asyncio
async def test_narrate_empty_candidates_returns_empty():
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    result = await narrator.narrate([])
    assert result == []
