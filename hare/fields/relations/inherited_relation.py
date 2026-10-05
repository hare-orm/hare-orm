from __future__ import annotations

from typing import Any


class InheritedRelation:
    """The relation of a concrete model's property its concrete subclass would inherit from Python - the
    subclass is a table of its own, which no row of the relation references, so it has no such relation.

    Args:
        name: The relation's name.
        base_model: The model the relation is of.
    """

    __slots__ = ("base_model", "name")

    def __init__(self, name: str, base_model: type) -> None:
        self.name = name
        self.base_model = base_model

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        model = owner if obj is None else type(obj)
        model_name = model.__name__ if model is not None else "the model"
        raise AttributeError(
            f"{model_name} has no relation {self.name!r} - it is {self.base_model.__name__}'s, whose table "
            f"{model_name}'s rows aren't in"
        )

    def __set__(self, obj: Any, value: Any) -> None:
        self.__get__(obj, type(obj))
