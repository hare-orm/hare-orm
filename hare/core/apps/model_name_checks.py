from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import SchemaPartType
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.core.apps.declarations import TableIdentity


class ModelNameChecks:
    """Checks table and schema object name collisions across the models registered on each connection."""

    @staticmethod
    def _check_table_name_collisions(apps: Apps) -> None:
        """Refuses two registered models resolving to the same table on one connection.

        Raises:
            ConfigurationError: Two models share a table.
        """
        # Two models defaulting to one table name are rejected here, naming both - per (connection,
        # schema), as schema generation groups them.
        model_by_table: dict[TableIdentity, type[Model]] = {}
        for app in apps.apps.values():
            for model in app.values():
                binding = apps.get_binding(model)
                if binding.swapped is not None:
                    continue
                table_key = apps.get_table_identity(model, binding.schema, binding.db_table)
                colliding_model = model_by_table.get(table_key)
                if colliding_model is not None and colliding_model is not model:
                    raise ConfigurationError(
                        f"Models {apps.get_binding(colliding_model).full_name!r} and "
                        f"{binding.full_name!r} both "
                        f"resolve to table {binding.db_table!r} on connection "
                        f"{binding.default_connection!r} - give one of them an explicit Meta.table "
                        f"name."
                    )
                model_by_table[table_key] = model

    @staticmethod
    def _check_object_names(apps: Apps) -> None:
        """Refuse collisions using each dialect's physical object namespaces.

        Historical model aliases of one table may repeat its declarations. Different
        object classes conflict only when the database gives them the same namespace.
        """
        declared: dict[tuple[Any, ...], tuple[TableIdentity, type[Model]]] = {}
        for model in dict.fromkeys(apps.get_models_iterable()):
            meta = apps.get_binding(model)
            if meta.swapped is not None:
                continue
            literals = apps.get_identifier_rules(model)
            table_key = apps.get_table_identity(model, meta.schema, meta.db_table)
            objects = [(SchemaPartType.TABLE, meta.db_table)]
            objects.extend(
                (SchemaPartType.INDEX, item.name)
                for item in meta.constraints
                if isinstance(item, (UniqueConstraint, ExclusionConstraint)) and item.name
            )
            objects.extend(
                (SchemaPartType.INDEX, item.name) for item in meta.indexes if isinstance(item, Index) and item.name
            )
            objects.extend(
                (object_type, item.name)
                for object_type, items in (
                    (SchemaPartType.TRIGGER, meta.triggers),
                    (SchemaPartType.VIEW, meta.views),
                    (SchemaPartType.MATERIALIZED_VIEW, meta.materialized_views),
                    (SchemaPartType.DICTIONARY, meta.dictionaries),
                    (SchemaPartType.SEQUENCE, meta.sequences),
                    (SchemaPartType.FUNCTION, meta.functions),
                )
                for item in items
            )
            for object_type, name in objects:
                object_key = literals.get_schema_object_key(object_type, meta.schema, meta.db_table, name)
                if object_key is None:
                    continue
                key = (meta.default_connection, *object_key)
                previous = declared.get(key)
                if previous is not None:
                    previous_table, previous_model = previous
                    if previous_model is model or previous_table != table_key:
                        raise ConfigurationError(
                            f"Models {apps.get_binding(previous_model).full_name!r} and {meta.full_name!r} declare "
                            f"more than one object named {name!r} in the same database namespace "
                            f"on connection {meta.default_connection!r} - give each object a distinct name."
                        )
                else:
                    declared[key] = (table_key, model)
