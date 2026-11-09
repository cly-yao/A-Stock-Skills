"""Load skill scripts that do not live on a normal import path."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import astock_runtime


def load(rel, name):
    return astock_runtime.load_py(ROOT / rel, name)
