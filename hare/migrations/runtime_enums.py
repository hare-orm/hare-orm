"""Enums built while the application runs (``StrEnum("Status", {...})``) in migrations: such an
enum has no module attribute to import it from, so a migration declares it itself, and the
migration state compares it by what it holds rather than by the class."""

from __future__ import annotations

import importlib
import sys
from enum import Enum
from typing import Any

from hare.classes.class_path import ClassPath
from hare.exceptions import ConfigurationError


class RuntimeEnums:
    """Tells an enum a migration can import from one it has to declare, and what an enum holds."""

    @staticmethod
    def is_importable(enum_type: type[Enum]) -> bool:
        """Whether a migration can import an enum: it declares ``migration_import_path``, or its
        ``__module__`` and ``__qualname__`` lead to this very class.

        Args:
            enum_type: The enum.

        Returns:
            False for an enum built while the application runs, defined inside a function, or
            declared by a migration file.
        """
        if getattr(enum_type, "migration_import_path", None):
            return True
        qualname = getattr(enum_type, "__qualname__", "")
        module_name = getattr(enum_type, "__module__", None)
        if not module_name or not qualname or "<locals>" in qualname:
            return False
        if not all(part.isidentifier() for part in module_name.split(".")):
            # A migration file's own module (``app.migrations.0001_initial``) - no import statement
            # can name it, so an enum it declares is declared again by a migration built from it.
            return False
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                return False
        found: Any = module
        for part in qualname.split("."):
            found = getattr(found, part, None)
            if found is None:
                return False
        return found is enum_type

    @staticmethod
    def get_bases(enum_type: type[Enum]) -> tuple[type[Enum], type | None]:
        """The enum class an enum is built on (``StrEnum``, ``IntEnum``, ``IntFlag``, ``Enum``, an
        enum class without members of the application's own), and the data type mixed into it.

        Args:
            enum_type: The enum.

        Returns:
            The enum base, and the mixed-in data type (``str`` of ``class X(str, Enum)``) or None.

        Raises:
            ConfigurationError: The enum has several enum bases or several data types.
        """
        enum_bases = [base for base in enum_type.__bases__ if issubclass(base, Enum)]
        data_types = [base for base in enum_type.__bases__ if not issubclass(base, Enum)]
        if len(enum_bases) != 1 or len(data_types) > 1:
            raise ConfigurationError(
                f"Cannot write {enum_type.__qualname__} into a migration: its bases {enum_type.__bases__!r} aren't "
                "one enum class and at most one data type"
            )
        return enum_bases[0], data_types[0] if data_types else None

    @classmethod
    def get_content(cls, enum_type: type[Enum]) -> tuple[Any, ...]:
        """What an enum holds - its bases, its name and its members in order - equal for two enums
        built the same way, one class or two.

        Args:
            enum_type: The enum.

        Returns:
            ``(bases, name, ((member name, value), ...))``, the bases as dotted paths.
        """
        return (
            tuple(ClassPath.get(base) for base in enum_type.__bases__),
            enum_type.__name__,
            tuple((name, member.value) for name, member in enum_type.__members__.items()),
        )
