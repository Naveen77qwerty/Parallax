"""
Root conftest.py — adds src/ to sys.path so `import barbell` works
whether or not the package is installed in editable mode.
"""
import sys
from pathlib import Path

# Add src/ to the path so barbell is importable
_src = Path(__file__).resolve().parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
