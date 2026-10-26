from __future__ import annotations

import dataclasses
import inspect
import typing
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.exceptions import FieldError
from hare.query.generic_foreign_keys.constants import GENERIC_FOREIGN_KEY_LOOKUPS, GENERIC_FOREIGN_KEY_TYPE_KEY
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths
from hare.query.managers.base_manager import BaseManager
from hare.query.queryset.queryset import QuerySet
from hare.stubs.constants import (
    FILTERS_SUFFIX,
    LOOKUP_SEPARATOR,
    PRIMARY_KEY_ATTRIBUTE_NAME,
    QUERYSET_SUFFIX,
    UPDATES_SUFFIX,
    WRITES_SUFFIX,
)
from hare.stubs.stub_imports import StubImports
from hare.stubs.value_type_text import ValueTypeText
from hare.typing_info.declarations import ValueType
from hare.typing_info.filter_value_types import FilterValueTypes

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.lookup_info.lookup_info import LookupInfo


class ModelStub:
    """What a stub declares for one model: the keys its filters take (``<Model>Filters``), the
    values its writes take (``<Model>Writes``, ``<Model>Updates`` of ``update()`` taking expressions
    too), and its queryset taking them (``<Model>QuerySet``, the type of ``<Model>.objects``).

    Args:
        model: The model.
        dialect: The dialect of the model's connection - the lookups it runs are the keys.
        imports: The stub's imports.
        relation_depth: How many relations a filter key crosses at most.
    """

    def __init__(self, model: type[Model], dialect: Dialect, imports: StubImports, relation_depth: int) -> None:
        self.model = model
        self.dialect = dialect
        self.imports = imports
        self.relation_depth = relation_depth
        self.value_type_text = ValueTypeText(imports)
        self.model_name = model.__name__
        #: The names of the model's declarations - free names of the stub's module.
        self.filters_name = imports.declare(f"{self.model_name}{FILTERS_SUFFIX}")
        self.writes_name = imports.declare(f"{self.model_name}{WRITES_SUFFIX}")
        self.updates_name = imports.declare(f"{self.model_name}{UPDATES_SUFFIX}")
        self.queryset_name = imports.declare(f"{self.model_name}{QUERYSET_SUFFIX}")

    def get_paths(self, model: type[Model], prefix: str, depth: int) -> list[str]:
        """The field paths a filter key may start with - the model's fields, and the fields of the
        models its relations lead to, ``depth`` relations deep - back to a model crossed before too
        (``shop__items__name``, ``parent__parent__name``).

        Args:
            model: The model the paths start at.
            prefix: The path to the model.
            depth: How many more relations a path crosses.

        Returns:
            The paths.
        """
        paths = [f"{prefix}{name}" for name in model._meta.generic_foreign_key_fields]
        for name, field in model._meta.fields_map.items():
            path = f"{prefix}{name}"
            paths.append(path)
            related_model = getattr(field, "related_model", None)
            if depth > 0 and isinstance(related_model, type):
                paths.extend(self.get_paths(related_model, f"{path}__", depth - 1))
        return paths

    def get_filter_types(self) -> dict[str, ValueType]:
        """The type of the value of each filter key."""
        filter_types: dict[str, ValueType] = {}
        for path in [PRIMARY_KEY_ATTRIBUTE_NAME, *self.get_paths(self.model, "", self.relation_depth)]:
            try:
                lookups = self.get_generic_lookups(path) or self.model._meta.get_lookups(path, self.dialect)
            except FieldError:
                # A field no filter reads (a generic relation's own attribute).
                continue
            for suffix, lookup_info in lookups.items():
                filter_types[f"{path}__{suffix}" if suffix else path] = FilterValueTypes.get_filter_value_type(
                    lookup_info
                )
        return filter_types

    def get_generic_lookups(self, path: str) -> dict[str, LookupInfo]:
        """The keys a filter takes for a generic foreign key - on itself and on its ``__type``, with
        each lookup it takes.

        Args:
            path: The path to the field.

        Returns:
            The description of each key by its suffix after the path - none for a path that isn't a
            generic foreign key.
        """
        lookups: dict[str, LookupInfo] = {}
        for type_suffix in ("", GENERIC_FOREIGN_KEY_TYPE_KEY):
            for lookup in sorted(GENERIC_FOREIGN_KEY_LOOKUPS):
                suffix = LOOKUP_SEPARATOR.join(part for part in (type_suffix, lookup) if part)
                lookup_info = GenericForeignKeyPaths.get_lookup_info(
                    self.model, f"{path}{LOOKUP_SEPARATOR}{suffix}" if suffix else path
                )
                if lookup_info is None:
                    return {}
                lookups[suffix] = lookup_info
        return lookups

    def get_write_types(self) -> dict[str, ValueType]:
        """The type of the value of each field ``create()`` and ``update()`` take."""
        write_types: dict[str, ValueType] = {}
        meta = self.model._meta
        for name in meta.fields_db_projection:
            field = meta.fields_map[name]
            write_types[name] = ValueType(
                classes=(field.field_type, *field.get_accepted_value_types()),
                declared_in=(self.model, name),
                accepts_enum_values=True,
                nullable=field.null,
            )
        for name in self.get_direct_relation_names():
            relation = cast("RelationalField[Any]", meta.fields_map[name])
            write_types[name] = ValueType(models=(relation.related_model,), nullable=relation.null)
        for name, generic_field in meta.generic_foreign_key_fields.items():
            # A branch's row is taken as a dict too - {"type": <branch>, <key fields>}.
            write_types[name] = ValueType(
                classes=(dict,), models=tuple(generic_field.branch_by_model), nullable=generic_field.null
            )
        if meta.primary_key_attribute:
            key_type = meta.get_lookup_info(PRIMARY_KEY_ATTRIBUTE_NAME).value_type
            write_types[PRIMARY_KEY_ATTRIBUTE_NAME] = ValueType(classes=(key_type,))
        return write_types

    def get_update_types(self) -> dict[str, ValueType]:
        """The type of the value of each field ``update()`` takes - a write's, or an expression."""
        return {
            name: dataclasses.replace(value_type, accepts_expressions=True)
            for name, value_type in self.get_write_types().items()
        }

    def get_direct_relation_names(self) -> list[str]:
        """The model's foreign key and one-to-one relations, in the order the model declares them -
        a stub written twice is the same text."""
        meta = self.model._meta
        relation_names = meta.foreign_key_fields | meta.one_to_one_fields
        return [name for name in meta.fields_map if name in relation_names]

    def get_typed_dict(self, name: str, types_by_key: dict[str, ValueType]) -> list[str]:
        """The lines of a ``TypedDict`` whose keys are optional - the functional form takes any key."""
        typed_dict = self.imports.add("typing", "TypedDict")
        lines = [f"{name} = {typed_dict}(", f"    {name!r},", "    {"]
        lines.extend(
            f"        {key!r}: {self.value_type_text.get_text(value_type)},"
            for key, value_type in types_by_key.items()
        )
        lines.extend(["    },", "    total=False,", ")"])
        return lines

    def get_declarations(self) -> list[str]:
        """The lines the stub adds for the model."""
        model = self.imports.add_class(self.model)
        filters = self.filters_name
        writes = self.writes_name
        updates = self.updates_name
        queryset_name = self.queryset_name
        queryset = self.imports.add("hare.query.queryset.queryset", "QuerySet")
        any_name = self.imports.add("typing", "Any")
        unpack = self.imports.add("typing", "Unpack")
        self_name = self.imports.add("typing", "Self")
        overload = self.imports.add("typing", "overload")
        literal = self.imports.add("typing", "Literal")
        q_name = self.imports.add("hare.query.expressions", "Q")
        exists = self.imports.add("hare.query.expressions", "Exists")
        single = self.imports.add("hare.query.queryset.single_rows.query_set_single", "QuerySetSingle")
        update_query = self.imports.add("hare.query.statements.write.update_query", "UpdateQuery")
        lines = [
            *self.get_typed_dict(filters, self.get_filter_types()),
            "",
            *self.get_typed_dict(writes, self.get_write_types()),
            "",
            *self.get_typed_dict(updates, self.get_update_types()),
            "",
            f"class {queryset_name}({self.get_queryset_base_text(self.model._meta.manager.queryset_class)}):",
            f"    def filter(self, *args: {q_name} | {exists}, **kwargs: {unpack}[{filters}]) -> {self_name}: ...",
            f"    def exclude(self, *args: {q_name} | {exists}, **kwargs: {unpack}[{filters}]) -> {self_name}: ...",
            f"    @{overload}",
            f"    def get(self, *args: {q_name}, does_not_exist_exception: None, multiple_objects_returned_exception: "
            f"{any_name} = ..., **kwargs: {unpack}[{filters}]) -> {single}[{model} | None]: ...",
            f"    @{overload}",
            f"    def get(self, *args: {q_name}, does_not_exist_exception: {any_name} = ..., "
            f"multiple_objects_returned_exception: {any_name} = ..., **kwargs: {unpack}[{filters}]) -> "
            f"{single}[{model}]: ...",
            f"    async def create(self, **kwargs: {unpack}[{writes}]) -> {model}: ...",
            f"    async def get_or_create(self, defaults: dict[str, {any_name}] | None = None, "
            f"**kwargs: {unpack}[{filters}]) -> tuple[{model}, bool]: ...",
            f"    async def update_or_create(self, defaults: dict[str, {any_name}] | None = None, create_defaults: "
            f"dict[str, {any_name}] | None = None, **kwargs: {unpack}[{filters}]) -> tuple[{model}, bool]: ...",
            f"    def update(self, **kwargs: {unpack}[{updates}]) -> {update_query}: ...",
            *self.get_extension_method_lines(),
        ]
        for name, value_type in self.get_selected_types().items():
            selected = self.value_type_text.get_text(value_type)
            lines.extend(
                [
                    f"    @{overload}",
                    f"    def values_list(self, field: {literal}[{name!r}], /, *, flat: {literal}[True], "
                    f"named: {literal}[False] = False) -> {queryset}[{model}, {selected}, {any_name}]: ...",
                ]
            )
        lines.extend(
            [
                f"    @{overload}",
                f"    def values_list(self, *fields_: str, flat: bool = False, named: bool = False, **kwargs: "
                f"{any_name}) -> {queryset}[{model}, {any_name}, {any_name}]: ...",
            ]
        )
        return lines

    def get_extension_method_lines(self) -> list[str]:
        """The QuerySet methods the model's dialect registered (``QuerySetExtensions``) - each with the
        parameters of its call; a method taking a condition takes ``filter()``'s.

        Returns:
            The lines.
        """
        # Local import: the queryset package imports the models this module reads.
        from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions

        any_name = self.imports.add("typing", "Any")
        self_name = self.imports.add("typing", "Self")
        lines = []
        for name, implementations in sorted(QuerySetExtensions.registered.items()):
            implementation = implementations.get(type(self.dialect))
            if implementation is None:
                continue
            if implementation.takes_condition:
                q_name = self.imports.add("hare.query.expressions", "Q")
                exists = self.imports.add("hare.query.expressions", "Exists")
                unpack = self.imports.add("typing", "Unpack")
                filters = self.filters_name
                parameters_text = f"*args: {q_name} | {exists}, **kwargs: {unpack}[{filters}]"
            else:
                parameters_text = ModelStub.get_parameters_text(implementation.get_call_signature(), any_name)
            separator = ", " if parameters_text else ""
            lines.append(f"    def {name}(self{separator}{parameters_text}) -> {self_name}: ...")
        return lines

    @staticmethod
    def get_parameters_text(signature: inspect.Signature, any_name: str) -> str:
        """The parameters of a stub's method - each of any type, a default written ``...``.

        Args:
            signature: The parameters.
            any_name: The stub's name of ``typing.Any``.

        Returns:
            The text.
        """
        parts: list[str] = []
        keyword_only_marked = False
        positional_only_open = False
        for parameter in signature.parameters.values():
            if positional_only_open and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY:
                parts.append("/")
            positional_only_open = parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                parts.append(f"*{parameter.name}: {any_name}")
                keyword_only_marked = True
            elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
                parts.append(f"**{parameter.name}: {any_name}")
            else:
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY and not keyword_only_marked:
                    parts.append("*")
                    keyword_only_marked = True
                default = "" if parameter.default is inspect.Parameter.empty else " = ..."
                parts.append(f"{parameter.name}: {any_name}{default}")
        if positional_only_open:
            parts.append("/")
        return ", ".join(parts)

    def get_selected_types(self) -> dict[str, ValueType]:
        """The type of each field ``values_list(name, flat=True)`` selects."""
        selected_types: dict[str, ValueType] = {}
        for name in self.model._meta.fields_db_projection:
            try:
                lookup_info = self.model._meta.get_lookup_info(name)
            except FieldError:
                continue
            selected_types[name] = FilterValueTypes.get_selected_value_type(lookup_info)
        return selected_types

    def get_attribute_annotations(self) -> dict[str, str]:
        """The annotation of each field of the model's class."""
        return ModelStub.get_field_annotations(self.model, self.imports)

    @staticmethod
    def get_field_annotations(model: type[Model], imports: StubImports) -> dict[str, str]:
        """The annotation of each field of a model's class - abstract too - ``Field[<value>]``, a
        descriptor giving the field on the class and its value on an instance; a relation's value is
        the related object, ``Any`` while the relation names its model by a string.

        Args:
            model: The model.
            imports: The stub's imports.

        Returns:
            The annotations by field name.
        """
        value_type_text = ValueTypeText(imports)
        field_class = imports.add("hare.fields.field", "Field")
        meta = model._meta
        relation_names = meta.foreign_key_fields | meta.one_to_one_fields
        annotations: dict[str, str] = {}
        for name in meta.fields_db_projection:
            field = meta.fields_map[name]
            value_class = getattr(field, "enum_type", None) or field.get_python_type()
            value_type = ValueType(classes=(value_class,), nullable=field.null)
            annotations[name] = f"{field_class}[{value_type_text.get_text(value_type)}]"
        for name in meta.fields_map:
            if name not in relation_names:
                continue
            relation = cast("RelationalField[Any]", meta.fields_map[name])
            related_model = relation.related_model
            value_type = (
                ValueType(models=(related_model,), nullable=relation.null)
                if isinstance(related_model, type)
                else ValueType(is_any=True)
            )
            annotations[name] = f"{field_class}[{value_type_text.get_text(value_type)}]"
        return annotations

    def get_primary_key_lines(self) -> list[str]:
        """The lines declaring ``pk`` in the model's class - the key's value, a tuple of the key fields'
        values for a composite key, None also taken on assignment; none for a model without a key."""
        if not self.model._meta.primary_key_attribute:
            return []
        key_type = FilterValueTypes.get_selected_value_type(
            self.model._meta.get_lookup_info(PRIMARY_KEY_ATTRIBUTE_NAME)
        )
        key_text = self.value_type_text.get_text(key_type)
        name = PRIMARY_KEY_ATTRIBUTE_NAME
        return [
            "    @property",
            f"    def {name}(self) -> {key_text}: ...",
            f"    @{name}.setter",
            f"    def {name}(self, value: {self.value_type_text.get_union([key_text, 'None'])}) -> None: ...",
        ]

    def get_manager_declarations(self) -> dict[str, str]:
        """The line declaring each manager of the model's class (``objects`` and the ones the class
        declares besides) - the queryset it gives: the stub's queryset for the default manager's
        queryset class, else the class itself."""
        class_var = self.imports.add("typing", "ClassVar")
        default_queryset_class = self.model._meta.manager.queryset_class
        managers: dict[str, BaseManager] = {}
        for model_class in reversed(self.model.__mro__):
            managers.update(
                (name, value) for name, value in vars(model_class).items() if isinstance(value, BaseManager)
            )
        declarations: dict[str, str] = {}
        for name, manager in managers.items():
            queryset_class = getattr(manager, "queryset_class", default_queryset_class)
            queryset_text = (
                self.queryset_name
                if queryset_class is default_queryset_class
                else self.get_queryset_base_text(queryset_class)
            )
            declarations[name] = f"    {name}: {class_var}[{queryset_text}]"
        return declarations

    @staticmethod
    def get_declared_type_parameters(declared_class: type) -> list[TypeVar]:
        """The type parameters a class declares - the type variables its bases take. The ones a base
        holds as the default of its own parameter (``QuerySet["Book"]`` holds its ``TRow``'s default
        ``TModel`` at run time) are no parameters of the class.

        Args:
            declared_class: The class.

        Returns:
            The type parameters, in order.
        """
        parameters: dict[TypeVar, None] = {}
        for base in getattr(declared_class, "__orig_bases__", ()):
            base_parameters = getattr(typing.get_origin(base), "__parameters__", ())
            for position, argument in enumerate(typing.get_args(base)):
                default = (
                    getattr(base_parameters[position], "__default__", None)
                    if position < len(base_parameters)
                    else None
                )
                if isinstance(argument, TypeVar) and argument is not default:
                    parameters[argument] = None
        return list(parameters)

    def get_queryset_base_text(self, queryset_class: type[QuerySet[Any, Any]]) -> str:
        """The annotation of a queryset class of the model - a class of the project's own as it is,
        a generic one given the model.

        Args:
            queryset_class: The class.

        Returns:
            The annotation.
        """
        model = self.imports.add_class(self.model)
        any_name = self.imports.add("typing", "Any")
        class_text = self.value_type_text.get_class_text(queryset_class)
        if queryset_class is QuerySet or class_text == any_name:
            return f"{self.imports.add('hare.query.queryset.queryset', 'QuerySet')}[{model}, {model}, {any_name}]"
        parameter_count = len(ModelStub.get_declared_type_parameters(queryset_class))
        if not parameter_count:
            return class_text
        # The model, its rows and the rest of any type.
        arguments = [model, model, *[any_name] * parameter_count][:parameter_count]
        return f"{class_text}[{', '.join(arguments)}]"
