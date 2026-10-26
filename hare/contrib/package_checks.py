from __future__ import annotations

from typing import Any

from hare.exceptions import ConfigurationError


class PackageChecks:
    """The check that a package a class of ``contrib`` works through is installed."""

    @staticmethod
    def require_package(package: Any, owner_name: str, package_name: str, extra_name: str) -> None:
        """Checks a package a class works through is installed.

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
