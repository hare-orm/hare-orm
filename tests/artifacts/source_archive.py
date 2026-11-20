from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

from tests.artifacts.constants import SOURCE_DOWNLOAD_TIMEOUT_SECONDS


class SourceArchive:
    """Cache immutable release URLs after a complete download, never a partial file.

    BuildSpec records the digest of the actual archive used by a build. The download
    cache is an input cache, not proof that a compiled artifact is compatible or complete.
    """

    @staticmethod
    def get(url: str, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / hashlib.sha256(url.encode()).hexdigest()
        if target.is_file():
            return target
        with tempfile.TemporaryDirectory(prefix="download-", dir=directory) as temporary:
            staged = Path(temporary) / "archive"
            with urllib.request.urlopen(url, timeout=SOURCE_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310
                staged.write_bytes(response.read())
            os.replace(staged, target)
        return target
