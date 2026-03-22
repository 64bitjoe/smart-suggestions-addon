import pytest
from scene_engine import SceneEngine, _remove_noops


def make_candidate(entity_id: str, score: float, type_: str = "entity", routine_match: bool = False) -> dict:
    domain = entity_id.split(".")[0]
    return {
        "entity_id": entity_id,
        "name": entity_id.split(".")[1],
        "domain": domain,
        "type": type_ if type_ != "entity" else ("scene" if domain == "scene" else "entity"),
        "current_state": "off",
        "score": score,
        "match_ratio": 0.0,
        "routine_match": routine_match,
        "reason": "test reason",
        "can_save_as_automation": routine_match and domain == "scene",
        "automation_context": None,
    }


def test_scenes_ranked_before_entities():
    engine = SceneEngine(max_suggestions=5)
    candidates = [
        make_candidate("light.kitchen", score=80),
        make_candidate("scene.evening", score=50, type_="scene"),
    ]
    result = engine.rank(candidates, states={}, feedback={})
    assert result[0]["entity_id"] == "scene.evening"


def test_max_suggestions_respected():
    engine = SceneEngine(max_suggestions=3, confidence_threshold=0.6)
    candidates = [make_candidate(f"light.l{i}", score=float(10 - i)) for i in range(10)]
    result = engine.rank(candidates, states={}, feedback={})
    assert len(result) <= 3


def test_hard_downvoted_entity_excluded():
    """Net vote of -3 or worse is excluded (boundary: exactly -3 excluded)."""
    engine = SceneEngine(max_suggestions=5)
    candidates = [make_candidate("light.kitchen", score=80)]
    # Exactly at boundary (-3): must be excluded
    feedback = {"light.kitchen": {"up": 0, "down": 3}}
    result = engine.rank(candidates, states={"light.kitchen": {"state": "off"}}, feedback=feedback)
    assert not any(c["entity_id"] == "light.kitchen" for c in result)


def test_two_downvotes_not_excluded():
    """Net vote of -2 should NOT be excluded."""
    engine = SceneEngine(max_suggestions=5)
    candidates = [make_candidate("light.kitchen", score=80)]
    feedback = {"light.kitchen": {"up": 0, "down": 2}}
    result = engine.rank(candidates, states={}, feedback=feedback)
    assert any(c["entity_id"] == "light.kitchen" for c in result)


def test_upvoted_entity_gets_score_boost():
    engine = SceneEngine(max_suggestions=5)
    candidates = [
        make_candidate("light.kitchen", score=50),
        make_candidate("light.bedroom", score=55),
    ]
    feedback = {"light.kitchen": {"up": 3, "down": 0}}
    result = engine.rank(candidates, states={}, feedback=feedback)
    kitchen = next(c for c in result if c["entity_id"] == "light.kitchen")
    bedroom = next(c for c in result if c["entity_id"] == "light.bedroom")
    assert kitchen["score"] > bedroom["score"]


def test_remove_noops_filters_already_on():
    states = {"light.kitchen": {"state": "on"}}
    candidates = [{"entity_id": "light.kitchen", "action": "turn_on", "current_state": "on"}]
    result = _remove_noops(candidates, states)
    assert result == []


def test_remove_noops_passes_scenes():
    """Scene activate actions are never filtered as noops."""
    states = {"scene.evening": {"state": "scening"}}
    candidates = [{"entity_id": "scene.evening", "action": "activate", "current_state": "scening"}]
    result = _remove_noops(candidates, states)
    assert len(result) == 1


def test_confidence_label_assigned():
    engine = SceneEngine(max_suggestions=5)
    candidates = [
        make_candidate("scene.evening", score=85, type_="scene", routine_match=True),
        make_candidate("light.kitchen", score=30),
    ]
    result = engine.rank(candidates, states={}, feedback={})
    scene = next(c for c in result if c["entity_id"] == "scene.evening")
    assert scene["confidence"] in ("high", "medium", "low")
