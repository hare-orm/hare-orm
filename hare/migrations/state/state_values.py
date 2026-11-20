from __future__ import annotations

from dataclasses import fields
from functools import partial
from types import BuiltinFunctionType, FunctionType, MethodType
from typing import Any

from hare.declarations.declaration_values import DeclarationValues
from hare.fields.field import Field
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State


class StateValues(DeclarationValues):
    """Full state declarations for validating an optimizer rewrite in one process.

    Unlike rename detection, comparison includes defaults, relation settings and
    every state option. Function identity is retained because preparation keeps
    callbacks unchanged; callbacks are never evaluated or compared by name alone.
    Rendering caches do not belong to the declaration.
    """

    @classmethod
    def same_field_except_index(cls, first: object, second: object) -> bool:
        """An index can move without changing the field's data, including its default and type."""
        return (
            isinstance(first, Field)
            and isinstance(second, Field)
            and cls._get_field_definition_without_index(first) == cls._get_field_definition_without_index(second)
        )

    @classmethod
    def _get_field_definition_without_index(cls, field: Field[Any]) -> object:
        path, args, kwargs = field.deconstruct()
        return cls.get((path, args, {key: value for key, value in kwargs.items() if key != "db_index"}))

    @classmethod
    def get_object_value(cls, value: Any) -> object:
        """Compare declared state fields and callback inputs without rendering code."""
        if isinstance(value, (State, ModelState)):
            return {
                attribute.name: cls.get(
                    dict(value.models.items())
                    if isinstance(value, State) and attribute.name == "models"
                    else getattr(value, attribute.name)
                )
                for attribute in fields(value)
                if attribute.init and attribute.compare
            }
        if isinstance(value, (FunctionType, BuiltinFunctionType, MethodType)):
            return value
        if isinstance(value, partial):
            return ["partial", cls.get(value.func), cls.get(value.args), cls.get(value.keywords)]
        return super().get_object_value(value)
