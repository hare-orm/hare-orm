from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.artifacts.constants import ARTIFACT_MANIFEST_VERSION, PYTHON_ABI_VARIABLES


@dataclass(frozen=True, slots=True)
class BuildSpec:
    """Content identity of an artifact, including sources and its actual build environment."""

    description: dict[str, Any]

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(self.description, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def digest(path: Path) -> str:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    @classmethod
    def create(
        cls,
        name: str,
        sources: Mapping[str, Path],
        *,
        options: Sequence[str] = (),
        compiler: Sequence[str] | None = None,
        python_abi: bool = False,
    ) -> BuildSpec:
        description: dict[str, Any] = {
            "manifest_version": ARTIFACT_MANIFEST_VERSION,
            "name": name,
            "sources": {name: cls.digest(path) for name, path in sources.items()},
            "platform": sysconfig.get_platform(),
            "machine": platform.machine(),
            "options": list(options),
        }
        if compiler is not None:
            command = [shutil.which(compiler[0]) or compiler[0], *compiler[1:]]
            result = subprocess.run([*command, "--version"], capture_output=True, text=True, check=True)
            description["compiler"] = {"command": command, "version": result.stdout}
        if python_abi:
            description["python"] = cls.get_python_abi()
        return cls(description)

    @staticmethod
    def get_python_abi() -> dict[str, Any]:
        return {
            "version": sys.version,
            "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            **{name: sysconfig.get_config_var(name) for name in PYTHON_ABI_VARIABLES},
        }
