from __future__ import annotations

from typing import Any, TypeVar

TBase = TypeVar("TBase")


class DeclaredSubclass:
    """Builds a subclass that only sets class attributes of its base - a SQL function known by its
    name and a few options is declared in one line instead of a class body of its own."""

    @staticmethod
    def make(base: type[TBase], name: str, module: str, doc: str | None = None, **attributes: Any) -> type[TBase]:
        """Creates the subclass.

        Args:
            base: The class it extends.
            name: Its name.
            module: The module it is declared in - a migration file imports it from there.
            doc: Its docstring.
            attributes: The class attributes it sets.

        Returns:
            The subclass.
        """
        return type(name, (base,), {"__module__": module, "__qualname__": name, "__doc__": doc, **attributes})
