from __future__ import annotations

from typing import Any

from hare.exceptions import ConfigurationError


class BrokerArguments:
    """The checks of what a delivery or a wakeup is set up with."""

    @staticmethod
    def require_package(package: Any, owner_name: str, package_name: str, extra_name: str) -> None:
        """Checks the package a delivery or a wakeup works through is installed.

        Args:
            package: The imported package, None when it isn't installed.
            owner_name: The class needing it.
            package_name: The package's name.
            extra_name: The extra of hare-orm installing it.

        Raises:
            ConfigurationError: The package isn't installed.
        """
        if package is None:
            raise ConfigurationError(f"{owner_name} needs the {package_name} package - install hare-orm[{extra_name}]")

    @staticmethod
    def require_text(name: str, value: Any, description: str = "string") -> None:
        """Checks an argument is a non-empty string.

        Args:
            name: The argument.
            value: Its value.
            description: What the string is.

        Raises:
            ConfigurationError: The value is no string, or an empty one.
        """
        if not isinstance(value, str) or not value:
            raise ConfigurationError(f"{name} must be a non-empty {description}, got {value!r}")
