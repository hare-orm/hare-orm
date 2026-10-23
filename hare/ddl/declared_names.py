from __future__ import annotations

from typing import Any

from hare.exceptions import ConfigurationError


class DeclaredNames:
    """The checks of the names a schema object is declared with - its fields, columns, roles, and its
    own name - refusing what isn't one before it is read as one."""

    @staticmethod
    def get_checked_names(names: Any, label: str) -> list[str] | None:
        """A list of names - of fields, columns, opclasses or roles.

        Args:
            names: The names, None for none.
            label: The argument, as the message names it.

        Returns:
            The names, None for None.

        Raises:
            ConfigurationError: ``names`` isn't a list or tuple of non-empty strings.
        """
        if names is None:
            return None
        if not isinstance(names, (list, tuple)) or not all(isinstance(name, str) and name for name in names):
            raise ConfigurationError(f"{label} takes a list of names, got {names!r}")
        return list(names)

    @staticmethod
    def raise_if_not_name(name: Any, label: str, *, optional: bool = False) -> None:
        """Refuses a name that isn't a non-empty string.

        Args:
            name: The name.
            label: The argument, as the message names it.
            optional: Whether None is taken.

        Raises:
            ConfigurationError: ``name`` isn't a non-empty string (or None, when optional).
        """
        if name is None and optional:
            return
        if not isinstance(name, str) or not name:
            raise ConfigurationError(f"{label} takes a name, got {name!r}")
