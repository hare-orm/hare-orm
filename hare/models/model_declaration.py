from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from hare.dialects.base.literals.constants import SQL_NULL_BYTE
from hare.exceptions import ConfigurationError
from hare.fields.constants import SCHEMA_TEXT_NULL_BYTE_MESSAGE
from hare.models.class_building.model_options import ModelOptions

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
    from hare.models.model import Model
    from hare.query.managers.manager import Manager


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ModelDeclaration:
    """The captured class declaration, independent of every prepared application graph.

    Class construction never creates a runtime binding. Fields and options are snapshots;
    relation linking, generated constraints, SQL and caches belong to ModelBinding instead.
    """

    model: type[Model]
    meta: type[Any]
    declared_options: Mapping[str, Any]
    fields_map: Mapping[str, Field[Any]]
    generic_foreign_key_fields: Mapping[str, GenericForeignKeyFieldInstance[Any]]
    primary_key_attribute: str | tuple[str, ...]
    declared_field_names: frozenset[str]
    pk_without_overlaps: bool
    manager: Manager[Any]
    abstract: bool
    app: str | None
    table_description: str
    tenant_field: str | None
    tenant_schema: bool

    @classmethod
    def capture(
        cls,
        model: type[Model],
        meta: type[Model.Meta],
        fields_map: dict[str, Field[Any]],
        generic_fields: dict[str, GenericForeignKeyFieldInstance[Any]],
        primary_key_attribute: str | tuple[str, ...],
        declared_field_names: frozenset[str],
        pk_without_overlaps: bool,
        manager: Manager[Any],
        table_description: str,
    ) -> ModelDeclaration:
        """Publish class inputs after field hooks and class comments have been applied."""
        meta = ModelOptions.snapshot(meta)
        meta.views = tuple(view.for_model(model) for view in meta.views)
        meta.materialized_views = tuple(view.for_model(model) for view in meta.materialized_views)
        declared_meta = vars(model).get("Meta")
        captured_options = vars(meta)
        declared_options = MappingProxyType(
            {
                name: captured_options[name]
                for name in ModelOptions.get_values(declared_meta)
                if name in captured_options
            }
            if declared_meta is not None
            else {}
        )
        if SQL_NULL_BYTE in table_description:
            raise ConfigurationError(SCHEMA_TEXT_NULL_BYTE_MESSAGE.format(owner="Meta", parameter="table_description"))
        memo: dict[int, Any] = {}
        fields = {name: field.snapshot(memo=memo) for name, field in fields_map.items()}
        generic_fields = {name: field.snapshot() for name, field in generic_fields.items()}
        for name, field in fields.items():
            field.model_field_name = name
            field.model = model
        for name, generic_field in generic_fields.items():
            generic_field.model = model
            generic_field.model_field_name = name
            for branch_name in generic_field.branch_names:
                if branch_name in fields:
                    fields[branch_name].generic_relation = generic_field  # type: ignore[attr-defined]
        return cls(
            model,
            meta,
            declared_options,
            MappingProxyType(fields),
            MappingProxyType(generic_fields),
            primary_key_attribute,
            declared_field_names,
            pk_without_overlaps,
            manager,
            bool(getattr(meta, "abstract", False) or not fields),
            getattr(meta, "app", None),
            table_description,
            getattr(meta, "tenant_field", None),
            bool(getattr(meta, "tenant_schema", False)),
        )
