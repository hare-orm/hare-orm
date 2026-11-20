from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


class ModuleLoader:
    """Execute a Python file under a test-selected module name."""

    @staticmethod
    def load(name: str, path: str | Path, monkeypatch: pytest.MonkeyPatch | None = None) -> ModuleType:
        """Register the module for the lifetime of the patch when one is supplied."""
        specification = importlib.util.spec_from_file_location(name, path)
        assert specification is not None and specification.loader is not None  # nosec B101
        module = importlib.util.module_from_spec(specification)
        if monkeypatch is not None:
            monkeypatch.setitem(sys.modules, specification.name, module)
        specification.loader.exec_module(module)
        return module
