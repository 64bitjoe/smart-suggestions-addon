import json
import pytest
from unittest.mock import AsyncMock, patch
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
async def test_narrate_preserves_candidate_count_appends_missing():
    """Ollama cannot remove candidates — missing items appended at end."""
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [
        make_candidate("scene.evening", "reason 1"),
        make_candidate("light.kitchen", "reason 2"),
    ]
    partial = json.dumps([{"entity_id": "scene.evening", "reason": "Better reason"}])

    with patch.object(narrator, "_call_ollama", new=AsyncMock(return_value=partial)):
        result = await narrator.narrate(candidates)

    assert len(result) == 2
    eids = [r["entity_id"] for r in result]
    assert "scene.evening" in eids
    assert "light.kitchen" in eids
    assert result[0]["reason"] == "Better reason"
    assert result[1]["reason"] == "reason 2"


@pytest.mark.asyncio
async def test_narrate_empty_candidates_returns_empty():
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    result = await narrator.narrate([])
    assert result == []


@pytest.mark.asyncio
async def test_narrate_accepts_context_kwarg():
    """narrate() must accept a context= kwarg without error."""
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [make_candidate("light.study", "original reason")]
    new_reasons = [{"entity_id": "light.study", "reason": "It has been on for an hour with no motion."}]

    context = {"current_time": "22:00 on Wednesday", "motion_sensors": [], "presence": ["person.john"],
               "weather": None, "avoided_pairs": [], "existing_automations": [], "recent_changes": []}

    with patch.object(narrator, "_call_ollama", new=AsyncMock(return_value=json.dumps(new_reasons))):
        result = await narrator.narrate(candidates, context=context)

    assert result[0]["reason"] == "It has been on for an hour with no motion."


@pytest.mark.asyncio
async def test_narrate_reranks_on_reordered_response():
    """If Ollama returns items in a different order, output respects that order."""
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [
        make_candidate("scene.evening", "reason 1"),
        make_candidate("light.kitchen", "reason 2"),
    ]
    reordered = json.dumps([
        {"entity_id": "light.kitchen", "reason": "Kitchen reordered reason"},
        {"entity_id": "scene.evening", "reason": "Evening reordered reason"},
    ])

    with patch.object(narrator, "_call_ollama", new=AsyncMock(return_value=reordered)):
        result = await narrator.narrate(candidates)

    assert result[0]["entity_id"] == "light.kitchen"
    assert result[1]["entity_id"] == "scene.evening"


@pytest.mark.asyncio
async def test_narrate_appends_missing_items_at_end_on_partial_rerank():
    """Items missing from Ollama reranked response are appended at the end."""
    narrator = OllamaNarrator(ollama_url="http://localhost:11434", model="llama3.2")
    candidates = [
        make_candidate("scene.evening", "reason 1"),
        make_candidate("light.kitchen", "reason 2"),
        make_candidate("switch.fan", "reason 3"),
    ]
    partial = json.dumps([
        {"entity_id": "light.kitchen", "reason": "Kitchen reason"},
        {"entity_id": "scene.evening", "reason": "Evening reason"},
    ])

    with patch.object(narrator, "_call_ollama", new=AsyncMock(return_value=partial)):
        result = await narrator.narrate(candidates)

    assert len(result) == 3
    assert result[0]["entity_id"] == "light.kitchen"
    assert result[1]["entity_id"] == "scene.evening"
    assert result[2]["entity_id"] == "switch.fan"
