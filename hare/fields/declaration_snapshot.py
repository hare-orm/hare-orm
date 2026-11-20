from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from gc import get_referents
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.fields.field import Field


class DeclarationSnapshot:
    """Copy declared values, retaining callbacks and snapshotting nested fields.

    A memo preserves shared children and container cycles during copying. Values
    supplied by custom declarations follow Python's deepcopy protocol unless
    they are callbacks or fields. No constructor, default or validator is run.
    """

    @staticmethod
    def copy_values(values: dict[str, Any], memo: dict[int, Any]) -> dict[str, Any]:
        DeclarationSnapshot.prepare_memo(values.values(), memo)
        return deepcopy(values, memo)

    @staticmethod
    def prepare_memo(values: Iterable[Any], memo: dict[int, Any]) -> None:
        """Register fields and callbacks through the declaration's actual object references.

        Python containers and custom wrappers share this walk, including slots and cycles.
        The subsequent deepcopy still decides how each wrapper itself is copied; fields and
        callbacks are leaves so their bound metadata and callback state are never traversed.
        """
        from hare.fields.field import Field

        pending = list(values)
        visited = set()
        while pending:
            value = pending.pop()
            identity = id(value)
            if identity in visited or identity in memo:
                continue
            visited.add(identity)
            if isinstance(value, Field):
                value.snapshot(memo=memo)
            elif callable(value) or isinstance(value, (classmethod, staticmethod, property)):
                memo[identity] = value
            else:
                pending.extend(get_referents(value))

    @staticmethod
    def field(field: Field[Any], memo: dict[int, Any] | None = None) -> Field[Any]:
        """Allocate a field declaration without invoking its constructor again."""
        memo = {} if memo is None else memo
        if id(field) in memo:
            return memo[id(field)]
        result = object.__new__(type(field))
        memo[id(field)] = result
        for name, value in DeclarationSnapshot.copy_values(field.get_declaration_values(), memo).items():
            object.__setattr__(result, name, value)
        return result
