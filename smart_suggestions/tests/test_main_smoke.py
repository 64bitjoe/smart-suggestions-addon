# smart_suggestions/tests/test_main_smoke.py
"""Smoke test: verify all new modules import and wire together without error."""
import pytest


def test_all_modules_importable():
    from const import _ACTION_DOMAINS
    from pattern_store import PatternStore
    from statistical_engine import StatisticalEngine
    from scene_engine import SceneEngine
    from narrator import OllamaNarrator, AINarrator
    from automation_builder import AutomationBuilder
    assert _ACTION_DOMAINS
    assert PatternStore
    assert StatisticalEngine
    assert SceneEngine
    assert OllamaNarrator
    assert AINarrator
    assert AutomationBuilder


def test_old_modules_do_not_exist():
    import importlib
    for mod in ("context_builder", "pattern_analyzer", "ollama_client",
                "anthropic_analyzer", "ollama_narrator"):
        try:
            importlib.import_module(mod)
            assert False, f"{mod} should have been deleted"
        except ModuleNotFoundError:
            pass
