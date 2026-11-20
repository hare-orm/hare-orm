from __future__ import annotations

from typing import TYPE_CHECKING

from mypy.nodes import ARG_NAMED, ARG_POS, CallExpr, ListExpr, MemberExpr, SetExpr, StrExpr, TupleExpr

from hare.contrib.mypy.annotation_types import AnnotationTypes
from hare.contrib.mypy.constants import (
    NOT_DIRECT_FIELD_MESSAGE,
    RELATION_FIELD_NAME_MESSAGE,
    UNKNOWN_NAME_MESSAGE,
)
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.exceptions import HareError
from hare.fields.constants import GENERIC_FOREIGN_KEY_TYPE_FIELD
from hare.query.constants import LOOKUP_SEPARATOR
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths
from hare.query.lookup_info.lookup_path import LookupPath

if TYPE_CHECKING:
    from mypy.plugin import MethodContext, Plugin
    from mypy.types import Type, TypedDictType

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.models.model_binding import ModelBinding


class FieldNamesCheck:
    """Checks the names given as string literals to ``order_by()``, ``only()``, ``defer()``,
    ``select_related()`` and ``bulk_update(fields=...)`` - the method's type stays as it is.

    Args:
        plugin: The plugin.
        model_registry: The bound models.
    """

    def __init__(self, plugin: Plugin, model_registry: ModelRegistry) -> None:
        self.plugin = plugin
        self.model_registry = model_registry

    def check_names(self, method_context: MethodContext) -> Type:
        """Reports the wrong names of the call.

        Args:
            method_context: The call.

        Returns:
            The method's own return type.
        """
        mypy_types = MypyTypes(self.plugin, method_context.api, self.model_registry)
        queryset_call = mypy_types.get_queryset_call(method_context.type, method_context.context)
        if queryset_call is None:
            return method_context.default_return_type
        model, queryset_type, call = queryset_call
        callee = call.callee
        if not isinstance(callee, MemberExpr):
            return method_context.default_return_type
        annotations = AnnotationTypes.get_annotations(method_context.type, queryset_type)
        method_name = callee.name
        for name in self.get_literal_names(call, method_name):
            if method_name == "order_by":
                self.check_ordering(mypy_types, model, annotations, name, call)
            elif method_name == "only":
                self.check_only(mypy_types, model, annotations, name, call)
            elif method_name == "defer":
                self.check_defer(mypy_types, model, name, call)
            elif method_name == "select_related":
                self.check_select_related(mypy_types, model, name, call)
            else:
                self.check_written_field(mypy_types, model, name, call)
        return method_context.default_return_type

    @staticmethod
    def get_literal_names(call: CallExpr, method_name: str) -> list[str]:
        """The names a call gives as string literals - its positional arguments, or the ``fields``
        list of ``bulk_update()``.

        Args:
            call: The call.
            method_name: The method.

        Returns:
            The names.
        """
        if method_name != "bulk_update":
            return [
                expression.value
                for argument_style, expression in zip(call.arg_kinds, call.args, strict=True)
                if argument_style == ARG_POS and isinstance(expression, StrExpr)
            ]
        for index, (name, argument_style, expression) in enumerate(
            zip(call.arg_names, call.arg_kinds, call.args, strict=True)
        ):
            if (
                (argument_style == ARG_NAMED and name == "fields") or (argument_style == ARG_POS and index == 1)
            ) and isinstance(expression, (ListExpr, TupleExpr, SetExpr)):
                return [item.value for item in expression.items if isinstance(item, StrExpr)]
        return []

    @staticmethod
    def check_ordering(
        mypy_types: MypyTypes, binding: ModelBinding, annotations: TypedDictType | None, name: str, call: CallExpr
    ) -> None:
        """Reports an ordering naming no field path or annotation.

        Args:
            mypy_types: The types of the call.
            binding: The queryset's model.
            annotations: The queryset's annotations, None when they aren't known.
            name: The ordering - ``?`` for a random order, a leading ``-`` for a descending one.
            call: The call.
        """
        path = name.removeprefix("-")
        if name == "?" or annotations is None or path.partition(LOOKUP_SEPARATOR)[0] in annotations.items:
            return
        generic_path = GenericForeignKeyPaths.get_generic_path(binding, path)
        if generic_path is not None and generic_path[2] == [GENERIC_FOREIGN_KEY_TYPE_FIELD]:
            # The branch set, read as a CASE over the branches.
            return
        try:
            binding.get_ordering_info(name)
        except HareError as error:
            mypy_types.fail(str(error), call)

    @staticmethod
    def check_only(
        mypy_types: MypyTypes, binding: ModelBinding, annotations: TypedDictType | None, name: str, call: CallExpr
    ) -> None:
        """Reports a field path of ``only()`` the model doesn't have, or one ending at a relation
        holding many rows - only a value of each row is loaded.

        Args:
            mypy_types: The types of the call.
            binding: The queryset's model.
            annotations: The queryset's annotations, None when they aren't known.
            name: The field path.
            call: The call.
        """
        model = binding._model
        if annotations is None or name.partition(LOOKUP_SEPARATOR)[0] in annotations.items:
            return
        try:
            binding.get_lookup_info(name)
        except HareError:
            mypy_types.fail(UNKNOWN_NAME_MESSAGE.format(method="only", model=model.__name__, name=name), call)
            return
        lookup_path = LookupPath.parse(binding, name, crosses_last=True)
        if not lookup_path.rest and lookup_path.relations and lookup_path.relations[-1].is_multi_valued:
            owner = lookup_path.relations[-2].related_model if len(lookup_path.relations) > 1 else model
            mypy_types.fail(
                RELATION_FIELD_NAME_MESSAGE.format(
                    method="only", model=owner.__name__, name=lookup_path.relation_names[-1], action="loaded"
                ),
                call,
            )

    @staticmethod
    def check_defer(mypy_types: MypyTypes, binding: ModelBinding, name: str, call: CallExpr) -> None:
        """Reports a name of ``defer()`` that isn't a direct field, a forward relation or a generic
        foreign key of the model.

        Args:
            mypy_types: The types of the call.
            binding: The queryset's model.
            name: The name.
            call: The call.
        """
        model = binding._model
        if name in binding.generic_foreign_key_fields:
            return
        if name not in binding.fields_map:
            mypy_types.fail(UNKNOWN_NAME_MESSAGE.format(method="defer", model=model.__name__, name=name), call)
        elif (
            name in binding.fetch_fields
            and name not in binding.foreign_key_fields
            and name not in binding.one_to_one_fields
        ):
            mypy_types.fail(NOT_DIRECT_FIELD_MESSAGE.format(model=model.__name__, name=name), call)

    @staticmethod
    def check_select_related(mypy_types: MypyTypes, binding: ModelBinding, name: str, call: CallExpr) -> None:
        """Report a path that cannot be joined through single-valued relations.

        Reverse one-to-one relations are accepted, and generic relation paths are expanded.

        Args:
            mypy_types: The types of the call.
            binding: The queryset's model.
            name: The path.
            call: The call.
        """
        try:
            for path in GenericForeignKeyPaths.expand_relation_paths(binding, (name,), "select_related"):
                LookupPath.check_select_related(binding, path)
        except HareError as error:
            mypy_types.fail(str(error), call)

    @staticmethod
    def check_written_field(mypy_types: MypyTypes, binding: ModelBinding, name: str, call: CallExpr) -> None:
        """Reports a name of ``bulk_update(fields=...)`` that isn't a field of the model's rows.

        Args:
            mypy_types: The types of the call.
            binding: The queryset's model.
            name: The name.
            call: The call.
        """
        model = binding._model
        if name in binding.generic_foreign_key_fields:
            return
        if name not in binding.fields_map:
            mypy_types.fail(UNKNOWN_NAME_MESSAGE.format(method="bulk_update", model=model.__name__, name=name), call)
        elif (
            name in binding.many_to_many_fields
            or name in binding.backward_foreign_key_fields
            or name in binding.backward_one_to_one_fields
        ):
            mypy_types.fail(
                RELATION_FIELD_NAME_MESSAGE.format(
                    method="bulk_update", model=model.__name__, name=name, action="updated"
                ),
                call,
            )
