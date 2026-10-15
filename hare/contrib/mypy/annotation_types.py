from __future__ import annotations

from typing import TYPE_CHECKING

from mypy.nodes import ARG_NAMED, ARG_STAR2, CallExpr, Expression, MemberExpr, RefExpr, StrExpr
from mypy.typeops import make_simplified_union
from mypy.types import Instance, NoneType, Type, TypedDictType, get_proper_type

from hare.contrib.mypy.constants import (
    ANNOTATION_TYPE_RULES,
    BOOL_FULLNAME,
    HIDDEN_ANNOTATIONS_ATTRIBUTE,
    INT_FULLNAME,
    QUERYSET_FULLNAME,
    TYPED_DICT_FALLBACK_FULLNAME,
)
from hare.contrib.mypy.enums import AnnotationTypeRule
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.exceptions import HareError

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.plugin import MethodContext, Plugin

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.models import Model


class AnnotationTypes:
    """The names and types of a queryset's ``.annotate()``/``.alias()`` expressions, kept in its type
    as a ``TypedDict`` - ``.alias()`` names as its not-required keys. A ``QuerySet`` keeps them in its
    ``TAnnotations`` parameter; a subclass that doesn't pass the parameter on keeps them where no
    attribute can reach them, so its own methods stay.

    Args:
        plugin: The plugin.
        model_registry: The bound models.
    """

    def __init__(self, plugin: Plugin, model_registry: ModelRegistry) -> None:
        self.plugin = plugin
        self.model_registry = model_registry

    @staticmethod
    def get_annotations(receiver: Type, queryset_type: Instance) -> TypedDictType | None:
        """The annotations of a queryset.

        Args:
            receiver: The type of the queryset.
            queryset_type: The queryset as a ``QuerySet[TModel, TRow, TAnnotations]``.

        Returns:
            The annotations, None when they aren't known (``Any``) - a name may then be one.
        """
        receiver = get_proper_type(receiver)
        if isinstance(receiver, Instance) and receiver.extra_attrs is not None:
            hidden_annotations = receiver.extra_attrs.attrs.get(HIDDEN_ANNOTATIONS_ATTRIBUTE)
            if hidden_annotations is not None:
                return AnnotationTypes.as_typed_dict(hidden_annotations)
        return AnnotationTypes.as_typed_dict(queryset_type.args[2])

    @staticmethod
    def as_typed_dict(annotations_type: Type) -> TypedDictType | None:
        """The annotations a type holds.

        Args:
            annotations_type: The type.

        Returns:
            The ``TypedDict``, None for anything else (``Any``).
        """
        proper_type = get_proper_type(annotations_type)
        if isinstance(proper_type, TypedDictType):
            return proper_type
        if isinstance(proper_type, Instance) and proper_type.type.typeddict_type is not None:
            return proper_type.type.typeddict_type
        return None

    def get_annotated_type(self, method_context: MethodContext) -> Type:
        """The type ``.annotate()``/``.alias()`` returns - the queryset with the new names.

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
        annotations = self.get_annotations(method_context.type, queryset_type)
        if annotations is None:
            return method_context.default_return_type
        if ARG_STAR2 in call.arg_kinds:
            # Names from a mapping aren't known - any name may be an annotation from now on.
            return self.with_annotations(return_type, queryset_type.args[1], mypy_types.any())
        is_alias = isinstance(call.callee, MemberExpr) and call.callee.name == "alias"
        new_types = {
            name: self.get_expression_type(mypy_types, model, expression)
            for name, argument_style, expression in zip(call.arg_names, call.arg_kinds, call.args, strict=True)
            if argument_style == ARG_NAMED and name is not None
        }
        items = {**annotations.items, **new_types}
        required_keys = set(annotations.required_keys) - set(new_types) | (set() if is_alias else set(new_types))
        typed_dict_fallback = method_context.api.named_generic_type(TYPED_DICT_FALLBACK_FULLNAME, [])
        new_annotations = TypedDictType(items, required_keys, set(), typed_dict_fallback)
        row_type = queryset_type.args[1]
        row_typed_dict = get_proper_type(row_type)
        if not is_alias and isinstance(row_typed_dict, TypedDictType):
            # A values() row selects the new annotations too.
            row_type = TypedDictType(
                {**row_typed_dict.items, **new_types},
                set(row_typed_dict.required_keys) | set(new_types),
                set(),
                typed_dict_fallback,
            )
        return self.with_annotations(return_type, row_type, new_annotations)

    @staticmethod
    def with_annotations(queryset: Instance, row_type: Type, annotations: Type) -> Instance:
        """A queryset type holding other annotations.

        Args:
            queryset: The queryset type.
            row_type: Its row type - a ``QuerySet`` takes it along.
            annotations: The annotations - a ``TypedDict``, or ``Any`` for unknown ones.

        Returns:
            The type.
        """
        if queryset.type.fullname == QUERYSET_FULLNAME:
            return queryset.copy_modified(args=[queryset.args[0], row_type, annotations])
        return queryset.copy_with_extra_attr(HIDDEN_ANNOTATIONS_ATTRIBUTE, annotations)

    @staticmethod
    def get_expression_type(mypy_types: MypyTypes, model: type[Model], expression: Expression) -> Type:
        """The type of an annotation's value by its expression.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            expression: The expression.

        Returns:
            The type - ``Any`` for an expression of no known rule.
        """
        if not isinstance(expression, CallExpr) or not isinstance(expression.callee, RefExpr):
            return mypy_types.any()
        rule = ANNOTATION_TYPE_RULES.get(expression.callee.fullname)
        if rule is AnnotationTypeRule.INT:
            return mypy_types.api.named_generic_type(INT_FULLNAME, [])
        if rule is AnnotationTypeRule.BOOL:
            return mypy_types.api.named_generic_type(BOOL_FULLNAME, [])
        if rule is AnnotationTypeRule.ARGUMENT_TYPE and expression.args:
            return mypy_types.api.get_expression_type(expression.args[0])
        if rule in {AnnotationTypeRule.PATH_VALUE, AnnotationTypeRule.OPTIONAL_PATH_VALUE} and expression.args:
            path = expression.args[0]
            if not isinstance(path, StrExpr):
                return mypy_types.any()
            try:
                path_type = mypy_types.get_selected_value_type(model._meta.get_lookup_info(path.value))
            except HareError:
                return mypy_types.any()
            if rule is AnnotationTypeRule.OPTIONAL_PATH_VALUE:
                return make_simplified_union([path_type, NoneType()])
            return path_type
        return mypy_types.any()
