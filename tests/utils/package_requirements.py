from __future__ import annotations

from tests.utils.constants import PACKAGE_REQUIREMENTS


class PackageRequirements:
    """What importing a module of hare requires - the extras of ``PACKAGE_REQUIREMENTS`` for the innermost
    package listed there holding it."""

    @staticmethod
    def get(module_name: str) -> tuple[str, ...]:
        """The extras a module of hare requires.

        Args:
            module_name: The module's dotted name - or a name inside it.

        Returns:
            The extras, none for a module of hare's own requirements.
        """
        packages = [
            package
            for package in PACKAGE_REQUIREMENTS
            if module_name == package or module_name.startswith(f"{package}.")
        ]
        return PACKAGE_REQUIREMENTS[max(packages, key=len)] if packages else ()
