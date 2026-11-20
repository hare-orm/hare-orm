from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hare.core.apps.relation_linking import RelationLinking
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.inspectdb.generation.inspected_model_builder import InspectedModelBuilder
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.models.class_building.model_field_collection import ModelFieldCollection

if TYPE_CHECKING:
    from hare.core.apps import Apps
    from hare.inspectdb.generation.inspected_model import InspectedModel
    from hare.models import Model


class InspectedModels:
    """Finalize relation identities before inspected declarations become classes or source.

    Catalog foreign keys name physical columns. Model relations name storage
    attributes, including allocated names and relation shadows. A generation run
    translates between these namespaces once and allocates reverse accessors in
    the actual target namespace, without reading or changing the active registry.
    """

    @staticmethod
    def prepare(builder: InspectedModelBuilder) -> InspectedModel:
        """Build one requested declaration with the explicitly supplied target context."""
        result = builder.build()
        options = replace(
            builder.options, class_name=None, target_tables=(), many_to_many_fields=(), field_names_by_column=None
        )
        prepared = [(builder, result)]
        for table in builder.options.target_tables:
            if table.name != builder.table.name:
                target_builder = InspectedModelBuilder(table, options)
                prepared.append((target_builder, target_builder.build()))
        InspectedModels.finalize(prepared, builder.options.target_models, apps=builder.options.apps)
        return result

    @staticmethod
    def finalize(
        prepared: list[tuple[InspectedModelBuilder, InspectedModel]],
        target_models: dict[str, type[Model]] | None = None,
        *,
        apps: Apps | None = None,
    ) -> None:
        """Translate target columns and allocate accessors using the completed field layouts."""
        if target_models and apps is None:
            from hare.core.hare_context import HareContext

            apps = HareContext.require_current().apps
        target_bindings = {reference: apps.get_binding(model) for reference, model in (target_models or {}).items()}  # type: ignore[union-attr]
        local = {builder.table.name: (builder, inspected) for builder, inspected in prepared if inspected.state}
        layouts: dict[str, dict[str, str]] = {}
        names: dict[str, set[str]] = {}
        reserved = ModelFieldCollection.get_reserved_field_names()
        for table, (builder, inspected) in local.items():
            state = inspected.state
            if state is None:
                continue
            columns = {}
            occupied = set(state.fields) | builder.used_attribute_names
            for column, attribute in builder.field_attribute_names.items():
                field = state.fields.get(attribute)
                if isinstance(field, ForeignKeyFieldInstance):
                    (storage_name,) = RelationLinking.get_key_field_names(attribute, ("id",))
                    columns[column] = storage_name
                    occupied.add(storage_name)
                elif field is not None and field.has_db_field:
                    columns[column] = attribute
            layouts[table] = columns
            names[table] = occupied

        external_names: dict[type[Model], set[str]] = {}
        for reference, model in (target_models or {}).items():
            if model not in external_names:
                external_names[model] = set(target_bindings[reference].fields_map) | {
                    attribute for base in model.__mro__ for attribute in vars(base)
                }

        for table, (builder, _inspected) in local.items():
            for composite_foreign_key in builder.table.composite_foreign_keys:
                reference = builder.get_foreign_key_target_reference(composite_foreign_key.target_table)
                target_model = (target_models or {}).get(reference)
                target_declaration = local.get(composite_foreign_key.target_table)
                if target_model is not None:
                    target_keys = target_bindings[reference].primary_key_attribute_names
                elif target_declaration is not None:
                    target_builder, _target = target_declaration
                    target_layout = layouts[composite_foreign_key.target_table]
                    target_keys = tuple(target_layout[column] for column in target_builder.pk_columns)
                else:
                    # The introspector recognized this shape using its catalog
                    # target. A supplied target context allows an exact check.
                    layouts[table].update(
                        zip(composite_foreign_key.columns, composite_foreign_key.columns, strict=True)
                    )
                    continue
                shadow_names = RelationLinking.get_key_field_names(composite_foreign_key.field_name, target_keys)
                if shadow_names != composite_foreign_key.columns:
                    raise ConfigurationError(
                        f"inspectdb: composite relation {table}.{composite_foreign_key.field_name} cannot reproduce "
                        f"its columns {composite_foreign_key.columns!r} "
                        f"using target attributes {target_keys!r}; "
                        "declare its key columns explicitly"
                    )
                layouts[table].update(zip(composite_foreign_key.columns, shadow_names, strict=True))
                names[table].update(shadow_names)

        target_names_by_reference = {}  # References to the same target share its reverse namespace.
        for builder, inspected in prepared:
            if inspected.state is None:
                continue
            for foreign_key in builder.foreign_keys.values():
                reference = builder.get_foreign_key_target_reference(foreign_key.target_table)
                target = (target_models or {}).get(reference)
                if target is not None:
                    target_names_by_reference[reference] = external_names[target]
                elif foreign_key.target_table in names:
                    target_names_by_reference[reference] = names[foreign_key.target_table]

        for builder, inspected in prepared:
            state = inspected.state
            if state is None:
                continue
            for foreign_key in builder.foreign_keys.values():
                if foreign_key.to_field is None:
                    continue
                reference = builder.get_foreign_key_target_reference(foreign_key.target_table)
                target_binding = target_bindings.get(reference)
                layout = (
                    target_binding.fields_db_projection_reverse
                    if target_binding is not None
                    else layouts.get(foreign_key.target_table)
                )
                if layout is None or foreign_key.to_field not in layout:
                    raise ConfigurationError(
                        f"inspectdb: target column {foreign_key.target_table}.{foreign_key.to_field} "
                        "requires target_tables or target_models containing its complete field layout"
                    )
                attribute = builder.field_attribute_names[foreign_key.column]
                field = state.fields[attribute]
                if isinstance(field, ForeignKeyFieldInstance):
                    field.to_field = layout[foreign_key.to_field]

        target_names_by_reference.update(
            (reference, external_names[model]) for reference, model in (target_models or {}).items()
        )
        # The first local declaration takes precedence for a repeated reference.
        for target_builder, target_inspected in reversed(local.values()):
            target_names_by_reference[f"{target_builder.app_label}.{target_inspected.class_name}"] = names[
                target_inspected.table_name
            ]

        edges = []
        for builder, inspected in prepared:
            state = inspected.state
            if state is None:
                continue
            for attribute, field in state.fields.items():
                if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)):
                    continue
                model_reference: str = field.model_name  # type: ignore[assignment]
                target_names = target_names_by_reference.setdefault(model_reference, set())
                preferred = RelationLinking.get_backward_relation_name(
                    field.related_name, builder.table.name, builder.app_label, inspected.class_name
                )
                if preferred is not None:
                    edges.append(
                        (builder.table.schema or "", builder.table.name, attribute, field, preferred, target_names)
                    )
        for _schema, _table, _attribute, field, preferred, target_names in sorted(edges, key=lambda edge: edge[:3]):
            safe = ModelNaming.get_field_name(preferred)
            allocated = ModelNaming.allocate_attribute_name(safe, target_names, reserved)
            if allocated != preferred or field.related_name is not None:
                field.related_name = allocated
