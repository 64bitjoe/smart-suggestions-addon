# smart_suggestions/tests/conftest.py
import sys
import os

# Add src/ to path so test imports work without packaging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
