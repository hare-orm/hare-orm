from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path


class BenchmarkArtifacts:
    """Stage all generated artifacts before replacing any published file.

    Each replacement is atomic on its destination filesystem. Generation or staging
    errors preserve every old artifact. This is not a filesystem transaction across
    the README, documentation and chart directories; a publication I/O failure is
    propagated and never reported as a successful refresh.
    """

    @staticmethod
    def publish(artifacts: dict[Path, str], obsolete: Iterable[Path] = ()) -> None:
        with tempfile.TemporaryDirectory(prefix="hare-benchmark-report-") as directory:
            staged = {}
            for index, (destination, content) in enumerate(artifacts.items()):
                source = Path(directory) / str(index)
                source.write_text(content, encoding="utf-8", newline="\n")
                staged[destination] = source
            for destination, source in staged.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                    temporary = Path(output.name)
                try:
                    shutil.copyfile(source, temporary)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            for path in obsolete:
                if path not in artifacts:
                    path.unlink(missing_ok=True)
