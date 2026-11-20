from __future__ import annotations

from typing import TYPE_CHECKING

from mypy.nodes import ARG_NAMED, CallExpr
from mypy.types import Type

from hare.contrib.mypy.constants import (
    MANY_ROWS_RELATION_WRITE_MESSAGE,
    REFUSED_UPDATE_NAME_MESSAGE,
    UNKNOWN_WRITE_NAME_MESSAGE,
)
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.models.write.rules.enums import WriteType
from hare.typing_info.write_value_types import WriteValueTypes

if TYPE_CHECKING:
    from mypy.plugin import FunctionSigContext, MethodSigContext, Plugin
    from mypy.types import CallableType, FunctionLike

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.models.model_binding import ModelBinding


class WriteArgumentsCheck:
    """Checks the keyword arguments of ``create()``, ``update()`` and the model constructor: each
    sets a field of the model - by its name, a relation's column (``author_id``), or ``pk`` - with
    a value of the field's type; ``update()`` takes an expression too.

    Args:
        plugin: The plugin.
        model_registry: The bound models.
    """

    def __init__(self, plugin: Plugin, model_registry: ModelRegistry) -> None:
        self.plugin = plugin
        self.model_registry = model_registry

    def get_method_signature(
        self, accepts_expressions: bool, method_signature_context: MethodSigContext
    ) -> FunctionLike:
        """The signature of ``create()``/``update()`` taking the call's fields by name.

        Args:
            accepts_expressions: Whether a value may be an expression (``update()``).
            method_signature_context: The call.

        Returns:
            The signature.
        """
        mypy_types = MypyTypes(self.plugin, method_signature_context.api, self.model_registry)
        queryset_call = mypy_types.get_queryset_call(method_signature_context.type, method_signature_context.context)
        if queryset_call is None:
            return method_signature_context.default_signature
        model, _, call = queryset_call
        return self.get_signature(
            mypy_types, model, method_signature_context.default_signature, call, accepts_expressions
        )

    def get_constructor_signature(
        self, model_fullname: str, function_signature_context: FunctionSigContext
    ) -> FunctionLike:
        """The signature of a model's constructor taking the call's fields by name.

        Args:
            model_fullname: The full name of the model class.
            function_signature_context: The call.

        Returns:
            The signature.
        """
        binding = self.model_registry.get_binding(model_fullname)
        call = function_signature_context.context
        if binding is None or not isinstance(call, CallExpr):
            return function_signature_context.default_signature
        mypy_types = MypyTypes(self.plugin, function_signature_context.api, self.model_registry)
        return self.get_signature(
            mypy_types,
            binding,
            function_signature_context.default_signature,
            call,
            accepts_expressions=False,
        )

    def get_signature(
        self,
        mypy_types: MypyTypes,
        binding: ModelBinding,
        signature: CallableType,
        call: CallExpr,
        accepts_expressions: bool,
    ) -> CallableType:
        """A signature taking the call's fields by name, each of its value type.

        Args:
            mypy_types: The types of the call.
            binding: The model.
            signature: The default signature.
            call: The call.
            accepts_expressions: Whether a value may be an expression (``update()``).

        Returns:
            The signature.
        """
        method_parameter_names = set(signature.arg_names)
        types_by_name = {}
        for name, argument_style in zip(call.arg_names, call.arg_kinds, strict=True):
            if argument_style != ARG_NAMED or name is None or name in method_parameter_names:
                continue
            types_by_name[name] = self.get_field_value_type(
                mypy_types, binding, name, call, accepts_expressions=accepts_expressions
            )
        return MypyTypes.get_signature_with_keywords(signature, call, types_by_name)

    @staticmethod
    def get_field_value_type(
        mypy_types: MypyTypes, binding: ModelBinding, name: str, call: CallExpr, *, accepts_expressions: bool = False
    ) -> Type:
        """The type of a value setting a field, reporting a name that sets none.

        Args:
            mypy_types: The types of the call.
            binding: The model.
            name: The name.
            call: The call.
            accepts_expressions: Whether this write can assign SQL expressions.

        Returns:
            The type - ``Any`` for a wrong name, reported.
        """
        model = binding._model
        description = WriteValueTypes.get(
            binding,
            name,
            accepts_expressions=accepts_expressions,
            write_type=WriteType.UPDATE if accepts_expressions else WriteType.INSERT,
        )
        if description is not None:
            return mypy_types.get_value_type(description)
        message = MANY_ROWS_RELATION_WRITE_MESSAGE if name in binding.fields_map else UNKNOWN_WRITE_NAME_MESSAGE
        if accepts_expressions and name in WriteValueTypes.get_names(binding):
            message = REFUSED_UPDATE_NAME_MESSAGE
        mypy_types.fail(message.format(model=model.__name__, name=name), call)
        return mypy_types.any()
