from __future__ import annotations

import dataclasses
from typing import Any

from hare.classes.class_path import ClassPath
from hare.classes.comparable_values import ComparableValues


class DeclaredAttributeValues(ComparableValues):
    """Comparable values in which a declaration written as a dataclass (a constraint, a trigger, a view)
    is compared by every argument it is built with, not by its ``deconstruct()`` - so an argument
    ``deconstruct()`` leaves out shows up as a difference once the declaration is read back from a
    migration file."""

    @classmethod
    def get_object_value(cls, value: Any) -> object:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return [
                ClassPath.get(type(value)),
                {
                    declared_field.name: cls.get(getattr(value, declared_field.name))
                    for declared_field in dataclasses.fields(value)
                    if declared_field.init
                },
            ]
        return super().get_object_value(value)
