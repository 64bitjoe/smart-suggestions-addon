# smart_suggestions/tests/test_main_smoke.py
"""Smoke test: verify surviving modules import without error."""
import pytest


def test_all_modules_importable():
    from const import _ACTION_DOMAINS
    from automation_builder import AutomationBuilder
    assert _ACTION_DOMAINS
    assert AutomationBuilder


def test_old_modules_do_not_exist():
    import importlib
    for mod in ("context_builder", "pattern_analyzer", "ollama_client",
                "anthropic_analyzer", "ollama_narrator",
                "pattern_store", "statistical_engine", "scene_engine", "narrator"):
        try:
            importlib.import_module(mod)
            assert False, f"{mod} should have been deleted"
        except ModuleNotFoundError:
            pass
