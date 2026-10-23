from __future__ import annotations

import json
import math
from enum import Enum
from types import FunctionType
from typing import Any

from hare.classes.class_path import ClassPath


class ComparableValues:
    """Values two equal declarations give alike - a declaration rebuilt from a migration file is a new
    object, and hare's expressions compare by identity, so declarations are compared by the call a
    migration file writes them as (``deconstruct()``)."""

    @classmethod
    def get(cls, value: Any) -> object:
        """``value`` as two equal declarations give it: a plain value as it is; a collection, value by
        value; any other object as ``get_object_value()`` gives it.

        Args:
            value: The value.

        Returns:
            The comparable value - plain values, lists and dicts of them.
        """
        if isinstance(value, float) and math.isnan(value):
            # NaN is unequal to itself - two declarations of it are still equal.
            return ["float", "nan"]
        if isinstance(value, (int, float, str, bool, type(None))):
            return value
        if isinstance(value, dict):
            return {str(key): cls.get(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls.get(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted((cls.get(item) for item in value), key=repr)
        return cls.get_object_value(value)

    @classmethod
    def get_object_value(cls, value: Any) -> object:
        """An object as two equal declarations give it: a class by its path; an object a migration file
        writes as a call (``deconstruct()``) by that call; any other object by its text.

        Args:
            value: The object.

        Returns:
            The comparable value.
        """
        if isinstance(value, type) and issubclass(value, Enum):
            # By what it holds - a migration file declares the enum anew, one class per file.
            bases, name, members = cls.get_enum_content(value)
            return [
                "enum",
                list(bases),
                name,
                [[member_name, cls.get(member_value)] for member_name, member_value in members],
            ]
        if isinstance(value, type):
            return ClassPath.get(value)
        deconstruct = getattr(value, "deconstruct", None)
        if callable(deconstruct):
            path, args, kwargs = deconstruct()
            return [path, cls.get(args), cls.get(kwargs)]
        if isinstance(value, FunctionType):
            # By its name - a migration file declares the function anew.
            return ["function", value.__name__]
        return str(value)

    @staticmethod
    def get_enum_content(enum_type: type[Enum]) -> tuple[tuple[str, ...], str, tuple[tuple[str, Any], ...]]:
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

    @classmethod
    def get_declaration_key(cls, declaration: Any, *, without_name: bool = False) -> str:
        """A hashable text of a declaration's comparable value.

        Args:
            declaration: An object with ``deconstruct()`` - an index, a constraint, a view.
            without_name: Leave its ``name`` out - what tells a renamed declaration.

        Returns:
            The text.
        """
        path, args, kwargs = declaration.deconstruct()
        if without_name:
            kwargs = {name: value for name, value in kwargs.items() if name != "name"}
        return json.dumps([path, cls.get(args), cls.get(kwargs)], sort_keys=True)

    @classmethod
    def are_equal(cls, first: Any, second: Any) -> bool:
        """Whether two declarations are equal by their comparable values.

        Args:
            first: A declaration.
            second: Another one.
        """
        return cls.get(first) == cls.get(second)
