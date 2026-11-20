from __future__ import annotations

import dataclasses
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.core.config import HareConfig


@dataclasses.dataclass(frozen=True)
class CLIContext:
    """The `hare` CLI's global options, passed to every command's ``run()``: where to load the
    Hare config from. Hand it to ``CommandContext.load_config()`` to get that config.

    Attributes:
        config: The ``-c``/``--config`` value - ``module.VARIABLE`` (``settings.HARE_ORM``) or the
            path of a .json/.yml config file - or None, when the config is located through the
            environment or ``pyproject.toml``.
    """

    config: str | None

    @cached_property
    def _loaded_config(self) -> HareConfig:
        """Load one normalized config for command discovery and execution."""
        from hare.cli.config_locator import ConfigLocator
        from hare.cli.exceptions import CLIError, CLIUsageError
        from hare.core.config import HareConfig
        from hare.core.config.config_secrets import ConfigSecrets
        from hare.exceptions import ConfigurationError

        config_value = self.config or ConfigLocator.locate()
        if not config_value:
            raise CLIUsageError(
                "You must specify HARE_ORM in option or env, or pyproject.toml [tool.hare]",
            )
        try:
            return HareConfig.load(config_value)
        except ConfigurationError as error:
            shown_value = (
                ConfigSecrets.mask_url_secrets(config_value) if isinstance(config_value, str) else config_value
            )
            raise CLIError(f"Invalid Hare ORM configuration {shown_value!r}: {error}") from None
