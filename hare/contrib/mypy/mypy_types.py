from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mypy.maptype import map_instance_to_supertype
from mypy.nodes import ARG_NAMED, ARG_STAR2, ArgKind as ArgumentStyle, CallExpr, Context, TypeInfo, Var
from mypy.typeops import make_simplified_union
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    LiteralType,
    NoneType,
    TupleType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)
from mypy.typevars import fill_typevars_with_any

from hare.contrib.mypy.constants import (
    FIELD_FULLNAME,
    HARE_QUERY_ERROR,
    ITERABLE_FULLNAME,
    LIST_FULLNAME,
    QUERYSET_FULLNAME,
    STR_FULLNAME,
    TUPLE_FULLNAME,
)
from hare.contrib.mypy.model_registry import ModelRegistry
from hare.query.enums import LookupValueShape
from hare.typing_info.bound_models import BoundModels
from hare.typing_info.constants import ALWAYS_ACCEPTED_VALUE_FULLNAMES
from hare.typing_info.filter_value_types import FilterValueTypes

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.plugin import CheckerPluginInterface, Plugin

    from hare.fields.field import Field
    from hare.models import Model
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.typing_info.declarations import ValueType


class MypyTypes:
    """hare's runtime types as mypy types - the types of filter values, selected values and field
    values, read from what mypy knows of the project's classes.

    Args:
        plugin: The plugin - it looks classes up by their full names.
        api: mypy's checker, for the call being checked.
        model_registry: The bound models.
    """

    def __init__(self, plugin: Plugin, api: CheckerPluginInterface, model_registry: ModelRegistry) -> None:
        self.plugin = plugin
        self.api = api
        self.model_registry = model_registry

    def fail(self, message: str, context: Context) -> None:
        """Reports an error of a hare query.

        Args:
            message: The error.
            context: Where the error is.
        """
        self.api.fail(message, context, code=HARE_QUERY_ERROR)

    def get_type_info(self, fullname: str) -> TypeInfo | None:
        """The class mypy knows by a full name.

        Args:
            fullname: The full name.

        Returns:
            The class, None when mypy doesn't know it.
        """
        symbol = self.plugin.lookup_fully_qualified(fullname)
        return symbol.node if symbol is not None and isinstance(symbol.node, TypeInfo) else None

    def get_class_type(self, fullname: str) -> Type | None:
        """The type of an instance of a class, its type parameters filled with ``Any``.

        Args:
            fullname: The full name of the class.

        Returns:
            The type, None when mypy doesn't know the class.
        """
        type_info = self.get_type_info(fullname)
        return None if type_info is None else fill_typevars_with_any(type_info)

    def get_python_type(self, value_type: Any) -> Type:
        """The mypy type of a runtime type - a class, a tuple of classes (a composite key), or None.

        Args:
            value_type: The runtime type.

        Returns:
            The type - ``Any`` for None, ``object`` and a class mypy doesn't know.
        """
        if isinstance(value_type, tuple):
            return TupleType(
                [self.get_python_type(item) for item in value_type],
                self.api.named_generic_type(TUPLE_FULLNAME, [self.any()]),
            )
        if not isinstance(value_type, type) or value_type is object:
            return self.any()
        return self.get_class_type(BoundModels.get_fullname(value_type)) or self.any()

    def get_model_type(self, model: type[Model]) -> Type | None:
        """The type of an instance of a model.

        Args:
            model: The model.

        Returns:
            The type, None when mypy doesn't know the model.
        """
        return self.get_class_type(BoundModels.get_fullname(model))

    def get_declared_field_type(self, model: type[Model], field_name: str) -> Type | None:
        """The type of a field's value as the model class declares it - the ``TValue`` of its
        ``Field[TValue]`` attribute, exact for a generic field (``JSONField[MyDict]``), or the type of
        an attribute a field factory declares as the value itself (``CharEnumField(MyEnum)`` is a
        ``MyEnum``).

        Args:
            model: The model.
            field_name: The field.

        Returns:
            The type, None when the class declares no such attribute or declares its value as ``Any``.
        """
        model_info = self.get_type_info(BoundModels.get_fullname(model))
        field_info = self.get_type_info(FIELD_FULLNAME)
        symbol = None if model_info is None else model_info.get(field_name)
        if symbol is None or field_info is None or not isinstance(symbol.node, Var) or symbol.node.type is None:
            return None
        declared_type = get_proper_type(symbol.node.type)
        if isinstance(declared_type, Instance) and declared_type.type.has_base(FIELD_FULLNAME):
            value_type = map_instance_to_supertype(declared_type, field_info).args[0]
        else:
            value_type = declared_type
        # A field subclass that names no value type (``class RatingField(IntField)``) declares Any.
        return None if isinstance(get_proper_type(value_type), AnyType) else value_type

    def get_queryset_arguments(self, receiver: Type) -> tuple[type[Model], Instance] | None:
        """The model of a queryset and the queryset as a ``QuerySet[TModel, TRow, TAnnotations]``.

        Args:
            receiver: The type a queryset method is called on.

        Returns:
            The bound model and the queryset type, None for a queryset of no bound model.
        """
        receiver = get_proper_type(receiver)
        queryset_info = self.get_type_info(QUERYSET_FULLNAME)
        if (
            queryset_info is None
            or not isinstance(receiver, Instance)
            or not receiver.type.has_base(QUERYSET_FULLNAME)
        ):
            return None
        queryset_type = map_instance_to_supertype(receiver, queryset_info)
        model_type = get_proper_type(queryset_type.args[0])
        if not isinstance(model_type, Instance):
            return None
        model = self.model_registry.get_model(model_type.type.fullname)
        return None if model is None else (model, queryset_type)

    def get_filter_value_type(self, lookup_info: LookupInfo) -> Type:
        """The type of the value a filter key takes - a value, an iterable or a two-item range of
        the compared type, or an expression, a term or a subquery.

        Args:
            lookup_info: The description of the key.

        Returns:
            The type.
        """
        return self.get_value_type(FilterValueTypes.get_filter_value_type(lookup_info))

    def get_selected_value_type(self, lookup_info: LookupInfo) -> Type:
        """The type of the value ``values()``/``values_list()`` select for a path.

        Args:
            lookup_info: The description of the path, as a filter key.

        Returns:
            The type - None included when the value can be missing.
        """
        return self.get_value_type(FilterValueTypes.get_selected_value_type(lookup_info))

    def get_value_type(self, value_type: ValueType) -> Type:
        """The mypy type of a value's type - its shape and the expressions it takes included.

        Args:
            value_type: The value's type.

        Returns:
            The type.
        """
        item_type = self.get_item_type(value_type)
        mypy_type: Type
        if value_type.shape is LookupValueShape.LIST:
            mypy_type = self.api.named_generic_type(ITERABLE_FULLNAME, [item_type])
        elif value_type.shape is LookupValueShape.RANGE:
            bound_type = make_simplified_union([item_type, NoneType()])
            mypy_type = UnionType.make_union(
                [
                    TupleType([bound_type, bound_type], self.api.named_generic_type(TUPLE_FULLNAME, [self.any()])),
                    self.api.named_generic_type(LIST_FULLNAME, [bound_type]),
                ]
            )
        else:
            mypy_type = item_type
        if value_type.accepts_expressions:
            return make_simplified_union([mypy_type, *self.get_always_accepted_types()])
        return mypy_type

    def get_item_type(self, value_type: ValueType) -> Type:
        """The mypy type of one value, without its shape - the type the model class declares for its
        field where it declares one.

        Args:
            value_type: The value's type.

        Returns:
            The type.
        """
        if value_type.is_any:
            return self.any()
        if value_type.literals:
            str_type = self.api.named_generic_type(STR_FULLNAME, [])
            return make_simplified_union([LiteralType(name, str_type) for name in value_type.literals])
        item_types: list[Type] = []
        declared_in = value_type.declared_in
        declared_type = None if declared_in is None else self.get_declared_field_type(*declared_in)
        if declared_in is not None and declared_type is not None:
            model, field_name = declared_in
            field = model._meta.fields_map[field_name]
            declared_type = self.without_none(declared_type)
            item_types.append(
                self.with_raw_enum_values(declared_type, field) if value_type.accepts_enum_values else declared_type
            )
        else:
            item_types.extend(self.get_python_type(cls) for cls in value_type.classes)
        for model in value_type.models:
            model_type = self.get_model_type(model)
            if model_type is not None:
                item_types.append(model_type)
            elif not value_type.classes:
                # A branch of a generic relation mypy doesn't know - any instance.
                item_types.append(self.any())
        if value_type.nullable:
            item_types.append(NoneType())
        return make_simplified_union(item_types) if item_types else self.any()

    def with_raw_enum_values(self, declared_type: Type, field: Field[Any]) -> Type:
        """The type an enum field takes - a member, or the value of one (``status="open"``); any
        other type as it is.

        Args:
            declared_type: The declared type of the field's value.
            field: The field.

        Returns:
            The type.
        """
        proper_type = get_proper_type(declared_type)
        item_types = proper_type.items if isinstance(proper_type, UnionType) else [proper_type]
        is_enum = any(
            isinstance(item_type := get_proper_type(item), Instance) and item_type.type.is_enum for item in item_types
        )
        if not is_enum or not isinstance(field.field_type, type):
            return declared_type
        return make_simplified_union([declared_type, self.get_python_type(field.field_type)])

    def get_always_accepted_types(self) -> list[Type]:
        """The types a filter or ``update()`` takes for any key - an expression, a term, a subquery.

        Returns:
            The types mypy knows of.
        """
        accepted_types = (self.get_class_type(fullname) for fullname in ALWAYS_ACCEPTED_VALUE_FULLNAMES)
        return [accepted_type for accepted_type in accepted_types if accepted_type is not None]

    @staticmethod
    def without_none(value_type: Type) -> Type:
        """A type without its None.

        Args:
            value_type: The type.

        Returns:
            The type, None left out of a union.
        """
        proper_type = get_proper_type(value_type)
        if isinstance(proper_type, UnionType):
            return make_simplified_union(
                [item for item in proper_type.items if not isinstance(get_proper_type(item), NoneType)]
            )
        return value_type

    @staticmethod
    def any() -> AnyType:
        """``Any`` the plugin gives where a type isn't known.

        Returns:
            The type.
        """
        return AnyType(TypeOfAny.special_form)

    @staticmethod
    def get_signature_with_keywords(
        signature: CallableType, call: CallExpr, types_by_name: dict[str, Type]
    ) -> CallableType:
        """A signature taking the call's keyword arguments by name, each of its type - the
        ``**kwargs`` stays only for a call passing ``**mapping``.

        Args:
            signature: The method's signature.
            call: The call.
            types_by_name: The type of each keyword argument.

        Returns:
            The signature.
        """
        arg_types: list[Type] = []
        argument_styles: list[ArgumentStyle] = []
        arg_names: list[str | None] = []
        star2_parameter = None
        for arg_type, argument_style, arg_name in zip(
            signature.arg_types, signature.arg_kinds, signature.arg_names, strict=True
        ):
            if argument_style == ARG_STAR2:
                star2_parameter = (arg_type, argument_style, arg_name)
                continue
            arg_types.append(arg_type)
            argument_styles.append(argument_style)
            arg_names.append(arg_name)
        for name, value_type in types_by_name.items():
            if name not in arg_names:
                arg_types.append(value_type)
                argument_styles.append(ARG_NAMED)
                arg_names.append(name)
        if star2_parameter is not None and ARG_STAR2 in call.arg_kinds:
            arg_types.append(star2_parameter[0])
            argument_styles.append(star2_parameter[1])
            arg_names.append(star2_parameter[2])
        return signature.copy_modified(arg_types=arg_types, arg_kinds=argument_styles, arg_names=arg_names)
