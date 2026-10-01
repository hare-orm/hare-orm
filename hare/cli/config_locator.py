import os
import tomllib
from pathlib import Path

from hare.cli.exceptions import CLIError
from hare.core.constants import ENV_HARE_ORM_CONFIG


class ConfigLocator:
    """Finds where the CLI's Hare configuration is - the environment variable, else pyproject.toml."""

    @staticmethod
    def locate(file: str = "pyproject.toml") -> str:
        """Get hare orm config from env or pyproject.toml.

        Args:
            file: toml file that contains tool.hare settings

        Returns:
            Module path and var name that stores the hare config.
        """
        if not (config := os.getenv(ENV_HARE_ORM_CONFIG, "")) and (p := Path(file)).exists():
            try:
                doc = tomllib.loads(p.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise CLIError(f"Cannot read {file}: {exc}") from None
            tool_section = doc.get("tool", {})
            hare_section = tool_section.get("hare", {}) if isinstance(tool_section, dict) else {}
            config = hare_section.get("hare_orm", "") if isinstance(hare_section, dict) else ""
            if not isinstance(config, str):
                raise CLIError(
                    f"[tool.hare] hare_orm in {file} must be a string like 'module.VARIABLE', "
                    f"got {type(config).__name__}"
                )
        return config
