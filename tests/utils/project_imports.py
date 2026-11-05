from __future__ import annotations

import importlib
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest


class ProjectImports:
    """Temporarily import one on-disk project's namespaces, restoring any displaced modules.

    Only top-level names present in this project's directory are isolated. Imports of
    installed libraries remain cached. Commands may rewrite their models between entries;
    bytecode is disabled so a same-size edit within one timestamp tick cannot read stale code.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def get_module_names(self) -> set[str]:
        return {
            path.stem if path.is_file() else path.name
            for path in self.root.glob("*")
            if path.suffix == ".py" or path.is_dir()
        }

    @contextmanager
    def isolate_modules(self) -> Generator[None]:
        """Restore this project's namespaces, including source files created inside the context."""
        names = self.get_module_names()
        previous = dict(sys.modules)
        for name in tuple(sys.modules):
            if name.partition(".")[0] in names:
                del sys.modules[name]
        importlib.invalidate_caches()
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(sys, "dont_write_bytecode", True)
                yield
        finally:
            names.update(self.get_module_names())
            for name in tuple(sys.modules):
                if name.partition(".")[0] in names:
                    del sys.modules[name]
            sys.modules.update({name: module for name, module in previous.items() if name.partition(".")[0] in names})
            importlib.invalidate_caches()

    @contextmanager
    def activate(self) -> Generator[None]:
        with self.isolate_modules(), pytest.MonkeyPatch.context() as patch:
            patch.syspath_prepend(str(self.root))
            patch.chdir(self.root)
            yield
