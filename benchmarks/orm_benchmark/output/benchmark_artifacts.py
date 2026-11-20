from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from contextlib import ExitStack
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
        with ExitStack() as cleanup:
            staged = {}
            for destination, content in artifacts.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", dir=destination.parent, delete=False
                ) as output:
                    temporary = Path(output.name)
                    cleanup.callback(temporary.unlink, missing_ok=True)
                    output.write(content)
                staged[destination] = temporary
            for destination, temporary in staged.items():
                os.replace(temporary, destination)
            for path in obsolete:
                if path not in artifacts:
                    path.unlink(missing_ok=True)
