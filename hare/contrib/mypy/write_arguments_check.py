from __future__ import annotations

from typing import TYPE_CHECKING

from mypy.nodes import ARG_NAMED, CallExpr
from mypy.typeops import make_simplified_union
from mypy.types import NoneType, Type
from mypy.types_utils import is_overlapping_none

from hare.contrib.mypy.constants import MANY_ROWS_RELATION_WRITE_MESSAGE, UNKNOWN_WRITE_NAME_MESSAGE
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.exceptions import HareError

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.plugin import FunctionSigContext, MethodSigContext, Plugin
    from mypy.types import CallableType, FunctionLike

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.models import Model


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
        queryset_arguments = mypy_types.get_queryset_arguments(method_signature_context.type)
        call = method_signature_context.context
        if queryset_arguments is None or not isinstance(call, CallExpr):
            return method_signature_context.default_signature
        return self.get_signature(
            mypy_types, queryset_arguments[0], method_signature_context.default_signature, call, accepts_expressions
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
        model = self.model_registry.get_model(model_fullname)
        call = function_signature_context.context
        if model is None or not isinstance(call, CallExpr):
            return function_signature_context.default_signature
        mypy_types = MypyTypes(self.plugin, function_signature_context.api, self.model_registry)
        return self.get_signature(
            mypy_types, model, function_signature_context.default_signature, call, accepts_expressions=False
        )

    def get_signature(
        self,
        mypy_types: MypyTypes,
        model: type[Model],
        signature: CallableType,
        call: CallExpr,
        accepts_expressions: bool,
    ) -> CallableType:
        """A signature taking the call's fields by name, each of its value type.

        Args:
            mypy_types: The types of the call.
            model: The model.
            signature: The default signature.
            call: The call.
            accepts_expressions: Whether a value may be an expression (``update()``).

        Returns:
            The signature.
        """
        own_names = set(signature.arg_names)
        types_by_name = {}
        for name, argument_style in zip(call.arg_names, call.arg_kinds, strict=True):
            if argument_style != ARG_NAMED or name is None or name in own_names:
                continue
            value_type = self.get_field_value_type(mypy_types, model, name, call)
            if accepts_expressions:
                value_type = make_simplified_union([value_type, *mypy_types.get_always_accepted_types()])
            types_by_name[name] = value_type
        return MypyTypes.get_signature_with_keywords(signature, call, types_by_name)

    @staticmethod
    def get_field_value_type(mypy_types: MypyTypes, model: type[Model], name: str, call: CallExpr) -> Type:
        """The type of a value setting a field, reporting a name that sets none.

        Args:
            mypy_types: The types of the call.
            model: The model.
            name: The name.
            call: The call.

        Returns:
            The type - ``Any`` for a wrong name, reported.
        """
        meta = model._meta
        if name == "pk":
            try:
                return mypy_types.get_python_type(meta.get_lookup_info(name).value_type)
            except HareError:
                return mypy_types.any()
        if name in meta.generic_foreign_key_fields:
            generic_field = meta.generic_foreign_key_fields[name]
            branch_types = [
                mypy_types.get_model_type(branch_model) or mypy_types.any()
                for branch_model in generic_field.branch_by_model
            ]
            return make_simplified_union([*branch_types, *([NoneType()] if generic_field.null else [])])
        field = meta.fields_map.get(name)
        if field is None:
            mypy_types.fail(UNKNOWN_WRITE_NAME_MESSAGE.format(model=model.__name__, name=name), call)
            return mypy_types.any()
        if (
            name in meta.many_to_many_fields
            or name in meta.backward_foreign_key_fields
            or name in meta.backward_one_to_one_fields
        ):
            mypy_types.fail(MANY_ROWS_RELATION_WRITE_MESSAGE.format(model=model.__name__, name=name), call)
            return mypy_types.any()
        none_types: list[Type] = [NoneType()] if field.null else []
        if name in meta.foreign_key_fields or name in meta.one_to_one_fields:
            related_type = mypy_types.get_model_type(getattr(field, "related_model", model))
            return make_simplified_union([related_type or mypy_types.any(), *none_types])
        declared_type = mypy_types.get_declared_field_type(model, name)
        if declared_type is not None:
            value_type = mypy_types.with_raw_enum_values(declared_type, field)
            # A factory declaring the value itself (``CharEnumField``) leaves None out.
            if none_types and not is_overlapping_none(value_type):
                return make_simplified_union([value_type, *none_types])
            return value_type
        return make_simplified_union([mypy_types.get_python_type(field.field_type), *none_types])
