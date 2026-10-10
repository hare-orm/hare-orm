from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError
from hare.stubs.constants import (
    FILTERS_SUFFIX,
    QUERYSET_SUFFIX,
    WRITES_SUFFIX,
)
from hare.stubs.stub_imports import StubImports
from hare.stubs.value_type_text import ValueTypeText
from hare.typing_info.declarations import ValueType
from hare.typing_info.filter_value_types import FilterValueTypes

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class ModelStub:
    """What a stub declares for one model: the keys its filters take (``<Model>Filters``), the
    values its writes take (``<Model>Writes``), and its queryset taking them (``<Model>QuerySet``,
    the type of ``<Model>.objects``).

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

    def get_paths(self, model: type[Model], prefix: str, depth: int, crossed: frozenset[type[Model]]) -> list[str]:
        """The field paths a filter key may start with - the model's fields, and the fields of the
        models its relations lead to, ``depth`` relations deep; a model already crossed is not
        crossed again.

        Args:
            model: The model the paths start at.
            prefix: The path to the model.
            depth: How many more relations a path crosses.
            crossed: The models already crossed.

        Returns:
            The paths.
        """
        paths = []
        for name, field in model._meta.fields_map.items():
            path = f"{prefix}{name}"
            paths.append(path)
            related_model = getattr(field, "related_model", None)
            if depth > 0 and isinstance(related_model, type) and related_model not in crossed:
                paths.extend(self.get_paths(related_model, f"{path}__", depth - 1, crossed | {model}))
        return paths

    def get_filter_types(self) -> dict[str, ValueType]:
        """The type of the value of each filter key."""
        filter_types: dict[str, ValueType] = {}
        for path in ["pk", *self.get_paths(self.model, "", self.relation_depth, frozenset())]:
            try:
                lookups = self.model._meta.get_lookups(path, self.dialect)
            except FieldError:
                # A field no filter reads (a generic relation's own attribute).
                continue
            for suffix, lookup_info in lookups.items():
                filter_types[f"{path}__{suffix}" if suffix else path] = FilterValueTypes.get_filter_value_type(
                    lookup_info
                )
        return filter_types

    def get_write_types(self) -> dict[str, ValueType]:
        """The type of the value of each field ``create()`` and ``update()`` take."""
        write_types: dict[str, ValueType] = {}
        meta = self.model._meta
        for name in meta.fields_db_projection:
            field = meta.fields_map[name]
            write_types[name] = ValueType(
                classes=(field.field_type,),
                declared_in=(self.model, name),
                accepts_enum_values=True,
                nullable=field.null,
            )
        for name in meta.foreign_key_fields | meta.one_to_one_fields:
            relation = cast("RelationalField[Any]", meta.fields_map[name])
            write_types[name] = ValueType(models=(relation.related_model,), nullable=relation.null)
        return write_types

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
        filters = f"{self.model_name}{FILTERS_SUFFIX}"
        writes = f"{self.model_name}{WRITES_SUFFIX}"
        queryset_name = f"{self.model_name}{QUERYSET_SUFFIX}"
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
            f"class {queryset_name}({queryset}[{model}, {model}, {any_name}]):",
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
            f"    def update(self, **kwargs: {unpack}[{writes}]) -> {update_query}: ...",
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
            implementation = implementations.get(self.dialect.name)
            if implementation is None:
                continue
            if implementation.takes_condition:
                q_name = self.imports.add("hare.query.expressions", "Q")
                exists = self.imports.add("hare.query.expressions", "Exists")
                unpack = self.imports.add("typing", "Unpack")
                filters = f"{self.model_name}{FILTERS_SUFFIX}"
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
        """The annotation of each field of the model's class - ``Field[<value>]``, a descriptor giving
        the field on the class and its value on an instance; a relation's value is the related
        object."""
        field_class = self.imports.add("hare.fields.field", "Field")
        meta = self.model._meta
        annotations: dict[str, str] = {}
        for name in meta.fields_db_projection:
            field = meta.fields_map[name]
            value_class = getattr(field, "enum_type", None) or field.field_type
            value_type = ValueType(classes=(value_class,), nullable=field.null)
            annotations[name] = f"{field_class}[{self.value_type_text.get_text(value_type)}]"
        for name in meta.foreign_key_fields | meta.one_to_one_fields:
            relation = cast("RelationalField[Any]", meta.fields_map[name])
            value_type = ValueType(models=(relation.related_model,), nullable=relation.null)
            annotations[name] = f"{field_class}[{self.value_type_text.get_text(value_type)}]"
        return annotations

    def get_objects_declaration(self) -> str:
        """The line declaring ``objects`` in the model's class."""
        class_var = self.imports.add("typing", "ClassVar")
        return f"    objects: {class_var}[{self.model_name}{QUERYSET_SUFFIX}]"
