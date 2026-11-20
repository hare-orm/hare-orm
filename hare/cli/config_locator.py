from __future__ import annotations

import os
import tomllib
from pathlib import Path

from hare.cli.exceptions import CLIError
from hare.core.constants import ENV_HARE_ORM_CONFIG


class ConfigLocator:
    """Finds where the CLI's Hare configuration is - the environment variable, else pyproject.toml."""

    @staticmethod
    def locate(file: str = "pyproject.toml") -> str:
        """The hare configuration named by the environment or ``pyproject.toml``.

        Args:
            file: toml file that contains tool.hare settings

        Returns:
            Module path and var name that stores the hare config.

        Raises:
            CLIError: The project file is unreadable or ``hare_orm`` is not a configuration reference.
        """
        if not (config := os.getenv(ENV_HARE_ORM_CONFIG, "")) and (config_path := Path(file)).exists():
            try:
                doc = tomllib.loads(config_path.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                raise CLIError(f"Cannot read {file}: {error}") from None
            tool_section = doc.get("tool", {})
            hare_section = tool_section.get("hare", {}) if isinstance(tool_section, dict) else {}
            config = hare_section.get("hare_orm", "") if isinstance(hare_section, dict) else ""
            if not isinstance(config, str):
                raise CLIError(f"{file}: [tool.hare] hare_orm must name a configuration module variable or file")
        return config
