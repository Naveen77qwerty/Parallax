"""
Root conftest.py — adds src/ to sys.path so `import barbell` works
whether or not the package is installed in editable mode.
"""
import sys
from pathlib import Path

import pytest

# Add src/ to the path so barbell is importable
_src = Path(__file__).resolve().parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    """Never let a test load the developer's real .env file.

    barbell.config.get_settings() calls load_dotenv(..., override=False),
    which — if a required var is unset at that moment — pulls it from a
    real (possibly empty) .env on disk and mutates os.environ permanently
    for the rest of the test run. Several code paths (e.g.
    endgame.schedule.current_phase()) call get_settings() inside a broad
    try/except that swallows the resulting error, so this leak can happen
    silently from a test that never touches env vars itself. Every test
    that needs settings already sets its own env vars explicitly, so the
    real .env must never leak in.
    """
    monkeypatch.setattr("barbell.config.load_dotenv", lambda *a, **k: None)
