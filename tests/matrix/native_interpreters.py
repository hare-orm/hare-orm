from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tests.matrix.matrix_catalog import MatrixCatalog
from tests.matrix.messages import NATIVE_INTERPRETER_MISSING


class NativeInterpreters:
    """Find the catalog's CPython interpreters installed on PATH by setup-python.

    Discovery is delegated to findpython and is imported only after build dependencies
    are installed. Catalog and virtualenv bootstrapping remain standard-library-only.
    """

    @staticmethod
    def get_paths() -> list[Path]:
        from findpython import Finder

        finder = Finder(selected_providers=["path"], no_same_interpreter=True)
        paths = []
        for version in MatrixCatalog.get("versions.python"):
            interpreter = finder.find(version, implementation="cpython")
            if interpreter is None:
                raise RuntimeError(NATIVE_INTERPRETER_MISSING.format(version=version))
            paths.append(interpreter.executable)
        return paths

    @classmethod
    def main(cls) -> None:
        parser = argparse.ArgumentParser(description="Find supported native-build interpreters on PATH.")
        parser.add_argument("--check-imports", action="store_true")
        arguments = parser.parse_args()
        paths = cls.get_paths()
        if arguments.check_imports:
            for path in paths:
                subprocess.run(
                    [str(path), "-c", "from rust.native import pg, rows; print(pg.__version__)"], check=True
                )
        else:
            # Shell command substitution must not retain a Windows carriage return in the last path.
            sys.stdout.reconfigure(newline="\n")
            print(" ".join(path.as_posix() for path in paths))


if __name__ == "__main__":
    NativeInterpreters.main()
