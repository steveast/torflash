"""Provide a mock PyQt5 so tests that import rutor_search don't fail
when PyQt5 is not installed (e.g. CI without a display server)."""

import sys
from unittest.mock import MagicMock

for mod in [
    "PyQt5", "PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
