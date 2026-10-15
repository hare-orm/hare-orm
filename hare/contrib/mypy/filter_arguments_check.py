from __future__ import annotations

from typing import TYPE_CHECKING

from mypy.nodes import ARG_NAMED, CallExpr
from mypy.typeops import make_simplified_union
from mypy.types import NoneType, Type

from hare.contrib.mypy.annotation_types import AnnotationTypes
from hare.contrib.mypy.constants import LOOKUP_SEPARATOR, UNSUPPORTED_FIELD_MESSAGE, UNSUPPORTED_LOOKUP_MESSAGE
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.exceptions import HareError
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.plugin import MethodSigContext, Plugin
    from mypy.types import FunctionLike, TypedDictType

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.lookup_info.lookup_info import LookupInfo


class FilterArgumentsCheck:
    """Checks the keyword arguments of ``filter()``/``exclude()``/``get()``/``get_or_create()``/
    ``update_or_create()``: each is a filter key of the queryset's model or one of its annotations,
    the lookup runs on the model's database, and the value is of the type the key takes.

    Args:
        plugin: The plugin.
        model_registry: The bound models.
    """

    def __init__(self, plugin: Plugin, model_registry: ModelRegistry) -> None:
        self.plugin = plugin
        self.model_registry = model_registry

    def get_signature(self, method_signature_context: MethodSigContext) -> FunctionLike:
        """The method's signature taking the call's filter keys by name, each of its value type.

        Args:
            method_signature_context: The call.

        Returns:
            The signature.
        """
        mypy_types = MypyTypes(self.plugin, method_signature_context.api, self.model_registry)
        queryset_arguments = mypy_types.get_queryset_arguments(method_signature_context.type)
        call = method_signature_context.context
        if queryset_arguments is None or not isinstance(call, CallExpr):
            return method_signature_context.default_signature
        model, queryset_type = queryset_arguments
        annotations = AnnotationTypes.get_annotations(method_signature_context.type, queryset_type)
        method_parameter_names = set(method_signature_context.default_signature.arg_names)
        types_by_name = {
            key: self.get_key_type(mypy_types, model, annotations, key, call)
            for key, argument_style in zip(call.arg_names, call.arg_kinds, strict=True)
            if argument_style == ARG_NAMED and key is not None and key not in method_parameter_names
        }
        return MypyTypes.get_signature_with_keywords(method_signature_context.default_signature, call, types_by_name)

    def get_key_type(
        self,
        mypy_types: MypyTypes,
        model: type[Model],
        annotations: TypedDictType | None,
        key: str,
        call: CallExpr,
    ) -> Type:
        """The type of the value a filter key takes, reporting a key the model doesn't have.

        Args:
            mypy_types: The types of the call.
            model: The queryset's model.
            annotations: The queryset's annotations, None when they aren't known.
            key: The filter key.
            call: The call.

        Returns:
            The type - ``Any`` for a wrong key, reported once.
        """
        name, _, lookup_suffix = key.partition(LOOKUP_SEPARATOR)
        if annotations is not None and name in annotations.items:
            if lookup_suffix:
                return mypy_types.any()
            return make_simplified_union(
                [annotations.items[name], NoneType(), *mypy_types.get_always_accepted_types()]
            )
        try:
            lookup_info = model._meta.get_lookup_info(key)
        except HareError as error:
            if annotations is not None:
                mypy_types.fail(str(error), call)
            return mypy_types.any()
        dialect = self.model_registry.get_dialect(model)
        if dialect is not None and not lookup_info.is_supported(dialect):
            mypy_types.fail(FilterArgumentsCheck.get_unsupported_message(model, key, lookup_info, dialect), call)
        return mypy_types.get_filter_value_type(lookup_info)

    @staticmethod
    def get_unsupported_message(model: type[Model], key: str, lookup_info: LookupInfo, dialect: Dialect) -> str:
        """Why a filter key doesn't run on a dialect - a field it reads has no column type there, or
        its last part isn't a lookup or a transform there.

        Args:
            model: The queryset's model.
            key: The filter key.
            lookup_info: The description of the key.
            dialect: The dialect of the model's connection.

        Returns:
            The message.
        """
        missing_fields = [
            field_object
            for field_object in (lookup_info.field, lookup_info.value_field)  # type: ignore[call-overload]
            if isinstance(field_object, Field) and not field_object.exists_on(dialect)
        ]
        if missing_fields or LOOKUP_SEPARATOR not in key:
            field = missing_fields[0] if missing_fields else lookup_info.field
            field_text = (
                f"{field.model.__name__}.{field.model_field_name}" if isinstance(field, Field) else model.__name__
            )
            return UNSUPPORTED_FIELD_MESSAGE.format(key=key, field=field_text, dialect=dialect.name)
        path, _, lookup_name = key.rpartition(LOOKUP_SEPARATOR)
        return UNSUPPORTED_LOOKUP_MESSAGE.format(
            key=key, model=model.__name__, path=path, lookup=lookup_name, dialect=dialect.name
        )
