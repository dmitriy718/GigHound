"""Make the repo root importable so `import worker...` works when pytest
runs from either the repo root or worker/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
