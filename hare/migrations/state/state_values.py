from __future__ import annotations

from dataclasses import fields
from functools import partial
from types import BuiltinFunctionType, FunctionType, MethodType
from typing import Any

from hare.declarations.declaration_values import DeclarationValues
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
    def get_object_value(cls, value: Any) -> object:
        """Compare declared state fields and callback inputs without rendering code."""
        if isinstance(value, (State, ModelState)):
            return {
                attribute.name: cls.get(getattr(value, attribute.name))
                for attribute in fields(value)
                if attribute.init and attribute.compare
            }
        if isinstance(value, (FunctionType, BuiltinFunctionType, MethodType)):
            return value
        if isinstance(value, partial):
            return ["partial", cls.get(value.func), cls.get(value.args), cls.get(value.keywords)]
        return super().get_object_value(value)
