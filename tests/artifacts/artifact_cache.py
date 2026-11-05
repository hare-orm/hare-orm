from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.artifacts.build_spec import BuildSpec
from tests.artifacts.constants import ARTIFACT_MANIFEST_NAME, ARTIFACT_MANIFEST_VERSION


class ArtifactCache:
    """Publish validated directories without replacing files already used by another process.

    Each build has a fresh staging directory. A successful build moves into a unique
    immutable directory, then atomically publishes its manifest. Interrupted builds and
    directories without a valid manifest are never reused. Concurrent publishers may
    leave an unused complete directory; they cannot expose a partially replaced library.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get(self, spec: BuildSpec) -> Path | None:
        try:
            manifest = json.loads((self.directory / f"{spec.key}.json").read_text(encoding="utf-8"))
            name = manifest["directory"]
            if not name or Path(name).name != name:
                return None
            directory = self.directory / name
            actual_spec = self.get_spec(directory)
            return directory if actual_spec is not None and actual_spec.description == spec.description else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def get_spec(directory: Path) -> BuildSpec | None:
        """Read provenance only when every published file is still present and intact."""
        try:
            manifest = json.loads((directory / ARTIFACT_MANIFEST_NAME).read_text(encoding="utf-8"))
            description = manifest["spec"]
            if not isinstance(description, dict) or description.get("manifest_version") != ARTIFACT_MANIFEST_VERSION:
                return None
            files = manifest["files"]
            actual = {
                path.relative_to(directory).as_posix()
                for path in directory.rglob("*")
                if path.is_file() and path.name != ARTIFACT_MANIFEST_NAME
            }
            if not isinstance(files, dict) or not files or set(files) != actual:
                return None
            if any(BuildSpec.digest(directory / name) != digest for name, digest in files.items()):
                return None
            return BuildSpec(description)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def prepare(self, spec: BuildSpec, build: Callable[[Path], None], verify: Callable[[Path], None]) -> Path:
        """Reuse an intact artifact, or publish a staged one after its load/version check."""
        if cached := self.get(spec):
            verify(cached)
            return cached
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="build-", dir=self.directory) as temporary:
            stage = Path(temporary) / "artifact"
            stage.mkdir()
            build(stage)
            verify(stage)
            files = {
                path.relative_to(stage).as_posix(): BuildSpec.digest(path)
                for path in stage.rglob("*")
                if path.is_file()
            }
            if not files:
                raise ValueError("An artifact build produced no files")
            self.write_json(stage / ARTIFACT_MANIFEST_NAME, {"spec": spec.description, "files": files})
            target = self.directory / f"{spec.key[:16]}-{uuid.uuid4().hex}"
            stage.rename(target)
            self.write_json(self.directory / f"{spec.key}.json", {"directory": target.name})
        return target

    @staticmethod
    def write_json(target: Path, value: Any) -> None:
        """Publish one manifest or artifact selection through an atomic file replacement."""
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(value, output, sort_keys=True, indent=2)
            except BaseException:
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
