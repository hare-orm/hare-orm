from __future__ import annotations

from typing import TYPE_CHECKING

from mypy.nodes import ARG_NAMED, ARG_POS, CallExpr, ListExpr, MemberExpr, SetExpr, StrExpr, TupleExpr

from hare.contrib.mypy.annotation_types import AnnotationTypes
from hare.contrib.mypy.constants import (
    LOOKUP_SEPARATOR,
    MULTI_VALUED_RELATION_MESSAGE,
    NOT_DIRECT_FIELD_MESSAGE,
    NOT_RELATION_MESSAGE,
    RELATION_FIELD_NAME_MESSAGE,
    RELATION_NOT_FOUND_MESSAGE,
    UNKNOWN_NAME_MESSAGE,
)
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.exceptions import HareError
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths
from hare.query.lookup_info.lookup_path import LookupPath

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.plugin import MethodContext, Plugin
    from mypy.types import Type, TypedDictType

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.models import Model


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
        queryset_arguments = mypy_types.get_queryset_arguments(method_context.type)
        call = method_context.context
        if queryset_arguments is None or not isinstance(call, CallExpr) or not isinstance(call.callee, MemberExpr):
            return method_context.default_return_type
        model, queryset_type = queryset_arguments
        annotations = AnnotationTypes.get_annotations(method_context.type, queryset_type)
        method_name = call.callee.name
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
        mypy_types: MypyTypes, model: type[Model], annotations: TypedDictType | None, name: str, call: CallExpr
    ) -> None:
        """Reports an ordering naming no field path or annotation.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            annotations: The queryset's annotations, None when they aren't known.
            name: The ordering - ``?`` for a random order, a leading ``-`` for a descending one.
            call: The call.
        """
        path = name.removeprefix("-")
        if name == "?" or annotations is None or path.partition(LOOKUP_SEPARATOR)[0] in annotations.items:
            return
        try:
            model._meta.get_ordering_info(name)
        except HareError as error:
            mypy_types.fail(str(error), call)

    @staticmethod
    def check_only(
        mypy_types: MypyTypes, model: type[Model], annotations: TypedDictType | None, name: str, call: CallExpr
    ) -> None:
        """Reports a field path of ``only()`` the model doesn't have.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            annotations: The queryset's annotations, None when they aren't known.
            name: The field path.
            call: The call.
        """
        if annotations is None or name.partition(LOOKUP_SEPARATOR)[0] in annotations.items:
            return
        try:
            model._meta.get_lookup_info(name)
        except HareError:
            mypy_types.fail(UNKNOWN_NAME_MESSAGE.format(method="only", model=model.__name__, name=name), call)

    @staticmethod
    def check_defer(mypy_types: MypyTypes, model: type[Model], name: str, call: CallExpr) -> None:
        """Reports a name of ``defer()`` that isn't a direct field of the model.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            name: The name.
            call: The call.
        """
        meta = model._meta
        if name not in meta.fields_map:
            mypy_types.fail(UNKNOWN_NAME_MESSAGE.format(method="defer", model=model.__name__, name=name), call)
        elif name in meta.fetch_fields:
            mypy_types.fail(NOT_DIRECT_FIELD_MESSAGE.format(model=model.__name__, name=name), call)

    @staticmethod
    def check_select_related(mypy_types: MypyTypes, model: type[Model], name: str, call: CallExpr) -> None:
        """Reports a path of ``select_related()`` that isn't made of forward relations.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            name: The path.
            call: The call.
        """
        try:
            paths = GenericForeignKeyPaths.expand_relation_paths(model, (name,), "select_related")
            for path in paths:
                lookup_path = LookupPath.parse(model, path, crosses_last=True)
                current_model = model
                for part, related_field in zip(lookup_path.relation_names, lookup_path.relations, strict=True):
                    if related_field.is_multi_valued:
                        mypy_types.fail(
                            MULTI_VALUED_RELATION_MESSAGE.format(name=part, model=current_model._meta.full_name), call
                        )
                        return
                    current_model = related_field.related_model
                if lookup_path.rest:
                    part = lookup_path.rest[0]
                    message = (
                        NOT_RELATION_MESSAGE if part in current_model._meta.fields_map else RELATION_NOT_FOUND_MESSAGE
                    )
                    mypy_types.fail(message.format(name=part, model=current_model._meta.full_name), call)
                    return
        except HareError as error:
            mypy_types.fail(str(error), call)

    @staticmethod
    def check_written_field(mypy_types: MypyTypes, model: type[Model], name: str, call: CallExpr) -> None:
        """Reports a name of ``bulk_update(fields=...)`` that isn't a field of the model's rows.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            name: The name.
            call: The call.
        """
        meta = model._meta
        if name not in meta.fields_map:
            mypy_types.fail(UNKNOWN_NAME_MESSAGE.format(method="bulk_update", model=model.__name__, name=name), call)
        elif (
            name in meta.many_to_many_fields
            or name in meta.backward_foreign_key_fields
            or name in meta.backward_one_to_one_fields
        ):
            mypy_types.fail(
                RELATION_FIELD_NAME_MESSAGE.format(
                    method="bulk_update", model=model.__name__, name=name, action="updated"
                ),
                call,
            )
