from __future__ import annotations

from typing import TYPE_CHECKING

from mypy.nodes import ARG_NAMED, ARG_POS, CallExpr, MemberExpr, NameExpr, StrExpr
from mypy.types import Instance, TupleType, Type, TypedDictType, get_proper_type

from hare.contrib.mypy.annotation_types import AnnotationTypes
from hare.contrib.mypy.constants import (
    FALSE_FULLNAME,
    TRUE_FULLNAME,
    TUPLE_FULLNAME,
    TYPED_DICT_FALLBACK_FULLNAME,
    UNKNOWN_NAME_MESSAGE,
)
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.exceptions import HareError
from hare.query.enums import Lookup

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.nodes import Expression
    from mypy.plugin import MethodContext, Plugin

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.models import Model


class ValuesRowTypes:
    """The rows of ``values()`` and ``values_list()``: a ``TypedDict`` of the selected names, a tuple
    of their types, or the one type with ``flat=True``. A ``named=True`` row stays ``Any``, and so do
    the rows of names that aren't string literals.

    Args:
        plugin: The plugin.
        model_registry: The bound models.
    """

    def __init__(self, plugin: Plugin, model_registry: ModelRegistry) -> None:
        self.plugin = plugin
        self.model_registry = model_registry

    def get_values_type(self, method_context: MethodContext) -> Type:
        """The queryset type ``values()``/``values_list()`` returns, its row type filled in.

        Args:
            method_context: The call.

        Returns:
            The type.
        """
        mypy_types = MypyTypes(self.plugin, method_context.api, self.model_registry)
        queryset_arguments = mypy_types.get_queryset_arguments(method_context.type)
        call = method_context.context
        return_type = get_proper_type(method_context.default_return_type)
        if queryset_arguments is None or not isinstance(call, CallExpr) or not isinstance(return_type, Instance):
            return method_context.default_return_type
        model, queryset_type = queryset_arguments
        annotations = AnnotationTypes.get_annotations(method_context.type, queryset_type)
        if annotations is None or not isinstance(call.callee, MemberExpr):
            return method_context.default_return_type
        method_name = call.callee.name
        is_values_list = method_name == "values_list"
        #: What each name selects - a string literal naming a path, an expression, or None for the
        #: path the name itself is.
        selected: dict[str, Expression | None] = {}
        flags: dict[str, bool] = {}
        for name, argument_style, expression in zip(call.arg_names, call.arg_kinds, call.args, strict=True):
            if argument_style == ARG_POS and isinstance(expression, StrExpr):
                selected[expression.value] = None
            elif argument_style == ARG_NAMED and is_values_list and name in {"flat", "named"}:
                if not isinstance(expression, NameExpr) or expression.fullname not in {TRUE_FULLNAME, FALSE_FULLNAME}:
                    return method_context.default_return_type
                flags[name] = expression.fullname == TRUE_FULLNAME
            elif argument_style == ARG_NAMED and name is not None:
                selected[name] = expression
            else:
                return method_context.default_return_type
        if flags.get("named"):
            return method_context.default_return_type
        if not selected:
            names = [name for name in model._meta.fields_map if name in model._meta.fields_db_projection]
            names += [name for name in annotations.items if name in annotations.required_keys]
            selected = dict.fromkeys(names)
        types_by_name = {
            name: self.get_selected_type(mypy_types, model, annotations.items, method_name, name, expression, call)
            for name, expression in selected.items()
        }
        if not is_values_list:
            row_type: Type = TypedDictType(
                types_by_name,
                set(types_by_name),
                set(),
                method_context.api.named_generic_type(TYPED_DICT_FALLBACK_FULLNAME, []),
            )
        elif flags.get("flat") and len(types_by_name) == 1:
            row_type = next(iter(types_by_name.values()))
        else:
            row_type = TupleType(
                list(types_by_name.values()), method_context.api.named_generic_type(TUPLE_FULLNAME, [mypy_types.any()])
            )
        # The result is a QuerySet, which keeps the annotations in its own parameter - a queryset class
        # of the project's own kept them out of sight.
        return return_type.copy_modified(args=[return_type.args[0], row_type, annotations])

    @staticmethod
    def get_selected_type(
        mypy_types: MypyTypes,
        model: type[Model],
        annotation_items: dict[str, Type],
        method_name: str,
        name: str,
        expression: Expression | None,
        call: CallExpr,
    ) -> Type:
        """The type of one selected value.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            annotation_items: The queryset's annotations.
            method_name: ``values`` or ``values_list``.
            name: The name the value is selected under.
            expression: A string literal naming a field path or an annotation, an expression, or
                None for the path the name itself is.
            call: The call.

        Returns:
            The type - ``Any`` for a wrong name, reported.
        """
        if expression is None:
            path = name
        elif isinstance(expression, StrExpr):
            path = expression.value
        else:
            return AnnotationTypes.get_expression_type(mypy_types, model, expression)
        if path in annotation_items:
            return annotation_items[path]
        try:
            lookup_info = model._meta.get_lookup_info(path)
        except HareError as error:
            mypy_types.fail(str(error), call)
            return mypy_types.any()
        if lookup_info.lookup is not Lookup.EXACT:
            mypy_types.fail(UNKNOWN_NAME_MESSAGE.format(method=method_name, model=model.__name__, name=path), call)
            return mypy_types.any()
        return mypy_types.get_selected_value_type(lookup_info)
