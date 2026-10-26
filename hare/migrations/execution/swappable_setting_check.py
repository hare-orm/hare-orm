from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ConfigurationError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.operations import CreateModel

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.migrations.loading.graph.migration_key import MigrationKey
    from hare.migrations.migration import Migration
    from hare.models import Model


class SwappableSettingCheck:
    """Refuses to migrate while a table an applied migration created has a foreign key declared with
    ``swappable()`` - a relation's own, or one of the automatic through table of a many-to-many relation -
    referencing another table than the one of the model its setting points at now: the setting changed
    after the table was created.

    Args:
        connection: The connection migrated.
        apps_config: The configuration of the apps migrated on it.
        disk_migrations: The migrations on disk, by key.
    """

    def __init__(
        self,
        connection: DatabaseClient,
        apps_config: Mapping[str, Any],
        disk_migrations: Mapping[MigrationKey, Migration],
    ) -> None:
        self.connection = connection
        self.apps_config = apps_config
        self.disk_migrations = disk_migrations

    async def check(self, applied: set[MigrationKey]) -> None:
        """Reads the tables of the live models declaring a swappable relation and compares their
        foreign keys with the setting.

        Args:
            applied: The applied migrations.

        Raises:
            ConfigurationError: Such a foreign key exists.
        """
        if not applied or not self.has_swappable_relations():
            return
        from hare.core.hare_context import HareContext
        from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

        live_context = HareContext.get_current()
        live_apps = live_context.apps if live_context is not None else None
        if live_apps is None:
            return
        # None stands for the connection's default schema, where a model without Meta.schema lives.
        models_by_schema: dict[str | None, list[type[Model]]] = {}
        for app_label in self.apps_config:
            for model in live_apps.apps.get(app_label, {}).values():
                if model._meta.managed is False or model._meta.swapped is not None:
                    continue
                models_by_schema.setdefault(model._meta.schema, []).append(model)
        mismatch_lines: list[str] = []
        for schema, models in models_by_schema.items():
            existing_tables = set(await DatabaseCatalog.get_table_names(self.connection, schema=schema))
            models = [model for model in models if model._meta.db_table in existing_tables]
            if not models:
                continue
            table_infos = await DatabaseCatalog.inspect_tables(
                self.connection, [model._meta.db_table for model in models], schema=schema, verify_exists=False
            )
            for model, table_info in zip(models, table_infos, strict=True):
                mismatch_lines.extend(
                    f"{model._meta.full_name}.{column_name}: {detail}"
                    for column_name, detail in self.get_foreign_key_mismatches(model._meta.fields_map, table_info)
                )
            through_relations = [
                (model, field)
                for model in models
                for field in model._meta.fields_map.values()
                if isinstance(field, ManyToManyFieldInstance)
                and isinstance(field.model_name, SwappableModelReference)
                and field.through_model is None
                and not field._generated
                and field.through in existing_tables
            ]
            if through_relations:
                through_infos = await DatabaseCatalog.inspect_tables(
                    self.connection,
                    [field.through for _model, field in through_relations],
                    schema=schema,
                    verify_exists=False,
                )
                for (model, field), through_info in zip(through_relations, through_infos, strict=True):
                    foreign_key_targets = {
                        column_name: foreign_key.target_table
                        for column_name, foreign_key in through_info.foreign_keys.items()
                    }
                    mismatch_lines.extend(
                        f"{model._meta.full_name}.{field.model_field_name} ({field.through}.{column_name}): {detail}"
                        for column_name, detail in self.get_through_table_mismatches(model, field, foreign_key_targets)
                    )
        if mismatch_lines:
            raise ConfigurationError(
                "A swappable model setting changed after its tables were created:\n  " + "\n  ".join(mismatch_lines)
            )

    def has_swappable_relations(self) -> bool:
        """Whether a migration on disk of the migrated apps declares a relation with ``swappable()``."""
        for key, migration in self.disk_migrations.items():
            if key.app_label not in self.apps_config:
                continue
            for operation in migration.operations:
                fields = (
                    [field for _name, field in operation.fields]
                    if isinstance(operation, CreateModel)
                    else [getattr(operation, "field", None)]
                )
                if any(isinstance(getattr(field, "model_name", None), SwappableModelReference) for field in fields):
                    return True
        return False

    @staticmethod
    def get_foreign_key_mismatches(
        model_fields: dict[str, Field[Any]], table_info: TableInfo
    ) -> list[tuple[str, str]]:
        """Lists the foreign keys declared with ``swappable()`` whose FOREIGN KEY constraint references
        another table than the one of the model the setting points at now.

        Args:
            model_fields: The current model's fields.
            table_info: The introspected table.

        Returns:
            (column name, what differs) pairs.
        """
        mismatches: list[tuple[str, str]] = []
        for field in model_fields.values():
            if not isinstance(field, ForeignKeyFieldInstance) or not isinstance(
                field.model_name, SwappableModelReference
            ):
                continue
            column_names = tuple(field.db_column_names)
            if len(column_names) == 1:
                foreign_key = table_info.foreign_keys.get(column_names[0])
                referenced_table = foreign_key.target_table if foreign_key is not None else None
            else:
                referenced_table = next(
                    (
                        composite_foreign_key.target_table
                        for composite_foreign_key in table_info.composite_foreign_keys
                        if set(composite_foreign_key.columns) == set(column_names)
                    ),
                    None,
                )
            detail = SwappableSettingCheck.get_foreign_key_mismatch(field, referenced_table)
            if detail is not None:
                mismatches.append((column_names[0], detail))
        return mismatches

    @staticmethod
    def get_through_table_mismatches(
        model: type[Model], field: ManyToManyFieldInstance[Any], foreign_key_targets: Mapping[str, str]
    ) -> list[tuple[str, str]]:
        """Lists the foreign keys of a swappable many-to-many relation's automatic through table referencing
        another table than the model's own and the one of the model the setting points at now.

        Args:
            model: The model declaring the relation.
            field: The relation.
            foreign_key_targets: The table each foreign key column of the through table references.

        Returns:
            (column name, what differs) pairs.
        """
        related_model = getattr(field, "related_model", None)
        if related_model is None:
            return []
        expected_tables = {model._meta.db_table, related_model._meta.db_table}
        setting_name = cast("SwappableModelReference", field.model_name).setting
        return [
            (
                column_name,
                f'its foreign key references table "{target_table}", but the '
                f'{setting_name} setting points at "{related_model._meta.full_name}" (table '
                f'"{related_model._meta.db_table}") - the setting changed after the table was created; move the '
                f"rows to the new model and repoint the foreign key with a migration of your own",
            )
            for column_name, target_table in foreign_key_targets.items()
            if target_table not in expected_tables
        ]

    @staticmethod
    def get_foreign_key_mismatch(field: Field[Any], referenced_table: str | None) -> str | None:
        """What differs when a foreign key declared with ``swappable()`` references another table than
        the one of the model the setting points at now.

        Args:
            field: The relation.
            referenced_table: The table its FOREIGN KEY constraint references.

        Returns:
            What differs, or None when the relation isn't swappable or references that model's table.
        """
        if not isinstance(field, ForeignKeyFieldInstance) or not isinstance(field.model_name, SwappableModelReference):
            return None
        related_model = getattr(field, "related_model", None)
        if related_model is None or referenced_table is None or referenced_table == related_model._meta.db_table:
            return None
        return (
            f'its foreign key references table "{referenced_table}", but the {field.model_name.setting} setting '
            f'points at "{related_model._meta.full_name}" (table "{related_model._meta.db_table}") - the setting '
            f"changed after the table was created; move the rows to the new model and repoint the foreign key "
            f"with a migration of your own"
        )
