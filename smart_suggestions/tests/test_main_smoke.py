# smart_suggestions/tests/test_main_smoke.py
"""Import-level smoke test: main.py wires only living modules."""


def test_main_imports():
    import main  # noqa: F401


def test_no_legacy_imports():
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "src" / "main.py").read_text()
    for legacy in ("narrator", "statistical_engine", "scene_engine",
                   "pattern_store", "usage_log", "dismissal_store",
                   "candidate_filter"):
        assert legacy not in src, f"main.py still references {legacy}"
