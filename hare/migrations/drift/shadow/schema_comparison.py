from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.core.apps.relation_linking import RelationLinking
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.security.grant import Grant
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.drift.column_mismatch import ColumnMismatch
from hare.migrations.drift.constants import DRIFT_NAMED_OBJECT_OPERATIONS, DRIFT_SCHEMA_OBJECT_PART_TYPES
from hare.migrations.drift.shadow.column_field_names import ColumnFieldNames
from hare.migrations.execution.swappable_setting_check import SwappableSettingCheck
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddGrant,
    AddIndex,
    AddPartition,
    AlterEnumType,
    AlterField,
    AlterModelOptions,
    AlterRowLevelSecurity,
    CreateEnumType,
    HareOperation,
    RemoveConstraint,
    RemoveGrant,
    RemoveIndex,
    RemovePartition,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class SchemaComparison:
    """The difference between the schema the models declare - built in a shadow and read back - and the
    database's, part by part: a part only the shadow has, only the database has, or that reads otherwise
    on the two becomes the operation that would make the database the models' - built from what the models
    declare.

    Args:
        new_state: The state the models declare.
        editor: A schema editor of the database's connection - it names the indexes and constraints.
        real_parts: The database's parts.
        shadow_parts: The shadow's parts, its names made the database's.
    """

    def __init__(
        self,
        new_state: State,
        editor: BaseSchemaEditor,
        real_parts: Iterable[SchemaPart],
        shadow_parts: Iterable[SchemaPart],
    ) -> None:
        self.new_state = new_state
        self.editor = editor
        self.real_parts = {part.key: part for part in real_parts}
        self.shadow_parts = {part.key: part for part in shadow_parts}
        # The parts of each type of each table, looked up for every table instead of walking every part.
        self.real_parts_by_table = SchemaComparison.get_parts_by_table(self.real_parts)
        self.shadow_parts_by_table = SchemaComparison.get_parts_by_table(self.shadow_parts)
        self.operations: list[HareOperation] = []
        self.untracked_columns: list[tuple[str, str, str, str]] = []
        self.mismatched_columns: list[ColumnMismatch] = []
        # One operation per field: adding it outweighs altering it.
        self.field_operations: dict[tuple[str, str, str], HareOperation] = {}

    def get_differences(
        self, part_type: SchemaPartType, table: str
    ) -> tuple[list[SchemaPart], list[SchemaPart], list[SchemaPart]]:
        """The parts of a type of a table only the shadow has, only the database has, and the shadow's of
        those the two have otherwise.

        Args:
            part_type: The parts' type.
            table: Their table - ``""`` for the schema's own.

        Returns:
            (only the shadow's, only the database's, the shadow's of the differing ones).
        """
        shadow_parts = self.shadow_parts_by_table.get((part_type, table), {})
        real_parts = self.real_parts_by_table.get((part_type, table), {})
        shadow_only = [part for key, part in shadow_parts.items() if key not in real_parts]
        real_only = [part for key, part in real_parts.items() if key not in shadow_parts]
        changed = [
            part
            for key, part in shadow_parts.items()
            if key in real_parts and part.definition != real_parts[key].definition
        ]
        return shadow_only, real_only, changed

    def has_table(self, table: str) -> bool:
        """Whether the database has a table - named as among every schema, its part by its own name."""
        return (SchemaPartType.TABLE, table) in self.real_parts_by_table

    @staticmethod
    def get_parts_by_table(
        parts: dict[tuple[Any, ...], SchemaPart],
    ) -> dict[tuple[SchemaPartType, str], dict[tuple[Any, ...], SchemaPart]]:
        """Parts grouped by their type and table.

        Args:
            parts: The parts by their keys.

        Returns:
            ``(type, table)`` -> the parts by their keys.
        """
        parts_by_table: dict[tuple[SchemaPartType, str], dict[tuple[Any, ...], SchemaPart]] = {}
        for key, part in parts.items():
            parts_by_table.setdefault((key[0], key[1]), {})[key] = part
        return parts_by_table

    def compare_model(
        self, key: tuple[str, str], model_state: ModelState, tables: Iterable[str], prefix: str = ""
    ) -> None:
        """Compares a model's tables.

        Args:
            key: The model's (app, name).
            model_state: Its declared state.
            tables: Its tables - the one storing its rows first, then one over it.
            prefix: What names a table of the model's schema among the parts of every schema.
        """
        app_label, model_name = key
        model = self.new_state.apps.get_model(f"{app_label}.{model_name}")
        tables = [f"{prefix}{table}" for table in tables]
        if not self.has_table(tables[0]):
            self.operations.append(OperationGenerator.get_create_model_operation(model_state))
            return
        field_name_by_column = ColumnFieldNames.column_to_field_name(model_state.fields)
        for table in tables:
            if not self.has_table(table):
                continue
            self.compare_columns(key, model_state, table, field_name_by_column)
            self.compare_primary_key(key, model_state, table)
            self.compare_indexes(key, model, model_state, table, field_name_by_column)
            self.compare_constraints(key, model, table)
            self.compare_foreign_keys(key, model, model_state, table, field_name_by_column)
            for part_type in (SchemaPartType.TRIGGER, SchemaPartType.POLICY, SchemaPartType.SEQUENCE):
                self.compare_named_objects(part_type, model, model_name, table)
            self.compare_grants(model_name, table)
            self.compare_table_options(model_name, model_state, table)
        self.compare_through_tables(key, model_state, prefix)

    def add_field_operation(
        self, key: tuple[str, str], model_state: ModelState, field_name: str, *, added: bool
    ) -> None:
        """Records the operation of one of a model's fields - its addition, else its alteration.

        Args:
            key: The model's (app, name).
            model_state: Its declared state.
            field_name: The field.
            added: Whether its column is missing.
        """
        operation_key = (key[0], key[1], field_name)
        existing = self.field_operations.get(operation_key)
        if existing is not None and (isinstance(existing, AddField) or not added):
            return
        field = model_state.fields[field_name]
        operation = (
            AddField(model_name=key[1], name=field_name, field=field)
            if added
            else AlterField(model_name=key[1], name=field_name, field=field)
        )
        if existing is not None:
            self.operations[self.operations.index(existing)] = operation
        else:
            self.operations.append(operation)
        self.field_operations[operation_key] = operation

    def compare_columns(
        self, key: tuple[str, str], model_state: ModelState, table: str, field_name_by_column: dict[str, str]
    ) -> None:
        """A column the database lacks is its field's ``AddField``, one read otherwise its ``AlterField``,
        one the model has no field for an untracked column."""
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.COLUMN, table)
        for part in shadow_only:
            self.add_field_operation(key, model_state, field_name_by_column[part.name], added=True)
        for part in changed:
            self.add_field_operation(key, model_state, field_name_by_column[part.name], added=False)
        for part in real_only:
            if part.name in field_name_by_column:
                # A column of a field the shadow lays out otherwise - its field is altered.
                self.add_field_operation(key, model_state, field_name_by_column[part.name], added=False)
            else:
                self.untracked_columns.append((key[0], key[1], model_state.table, part.name))

    def compare_primary_key(self, key: tuple[str, str], model_state: ModelState, table: str) -> None:
        """A primary key of other columns is a ``ColumnMismatch`` of each - no operation describes it."""
        shadow_part = self.shadow_parts.get((SchemaPartType.PRIMARY_KEY, table, table))
        real_part = self.real_parts.get((SchemaPartType.PRIMARY_KEY, table, table))
        if shadow_part is None or real_part is None or shadow_part.definition == real_part.definition:
            return
        declared_columns = shadow_part.value["columns"]
        real_columns = real_part.value["columns"]
        differing_columns = [column for column in declared_columns if column not in real_columns] + [
            column for column in real_columns if column not in declared_columns
        ]
        for column in differing_columns or declared_columns[:1]:
            if column in declared_columns and column not in real_columns:
                detail = "is not in the table's PRIMARY KEY, the model's primary key holds it"
            elif column in real_columns and column not in declared_columns:
                detail = "is in the table's PRIMARY KEY, the model's primary key doesn't hold it"
            else:
                detail = (
                    f"the table's PRIMARY KEY is {real_part.definition}, the model's primary key "
                    f"{shadow_part.definition}"
                )
            self.mismatched_columns.append(ColumnMismatch(key[0], key[1], model_state.table, column, detail))

    def get_index_owners(
        self, model: type[Model], model_state: ModelState, field_name_by_column: dict[str, str]
    ) -> dict[str, tuple[str, Any]]:
        """What declares each index and unique constraint of a model's table, by its name: a field's flag
        (``("field", field name)``), a ``Meta.indexes`` entry (``("index", Index)``) or a
        ``Meta.constraints`` one (``("constraint", constraint)``).

        Args:
            model: The model, rendered from the declared state.
            model_state: Its declared state.
            field_name_by_column: The field of the declared state owning each column.

        Returns:
            The owners by name.
        """
        owners: dict[str, tuple[str, Any]] = {}
        meta = model._meta

        for (owner_type, owner_id), (
            index,
            _is_constraint,
        ) in self.editor.generated_index_names.get_generated_index_names(model).items():
            if owner_type in ("field", "unique"):
                owners[str(index.name)] = (
                    "field",
                    self.get_state_field_name(model, model_state, field_name_by_column, owner_id),
                )
            elif owner_type == "index":
                entry = meta.indexes[owner_id]
                owners[str(index.name)] = ("index", entry if isinstance(entry, Index) else Index(fields=tuple(entry)))
            elif owner_type == "constraint":
                constraint = meta.constraints[owner_id]
                if RelationLinking.is_relation_uniqueness(model, constraint):
                    relation_name = next(
                        name
                        for name, field in meta.fields_map.items()
                        if tuple(getattr(field, "source_fields", ())) == tuple(constraint.fields)
                    )
                    owners[str(index.name)] = ("field", relation_name)
                else:
                    owners[str(index.name)] = ("constraint", constraint)
        for entry in meta.indexes:
            if isinstance(entry, Index) and entry.name:
                owners[entry.name] = ("index", entry)
        for constraint in meta.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.name:
                owners[constraint.name] = ("constraint", constraint)
        for field_name in model_state.fields:
            field = meta.fields_map.get(field_name)
            if field is not None and field.pk:
                owners.setdefault(f"pk:{field_name}", ("field", field_name))
        return owners

    @staticmethod
    def get_state_field_name(
        model: type[Model], model_state: ModelState, field_name_by_column: dict[str, str], field_name: str
    ) -> str:
        """The field of the declared state a field of the rendered model is - a relation's key field
        (``author_id``) is the relation (``author``).

        Args:
            model: The model, rendered from the declared state.
            model_state: Its declared state.
            field_name_by_column: The field of the declared state owning each column.
            field_name: The rendered model's field.

        Returns:
            The declared state's field.
        """
        if field_name in model_state.fields:
            return field_name
        column = model._meta.fields_db_projection.get(field_name, field_name)
        return field_name_by_column.get(column, field_name)

    def compare_indexes(
        self,
        key: tuple[str, str],
        model: type[Model],
        model_state: ModelState,
        table: str,
        field_name_by_column: dict[str, str],
    ) -> None:
        """An index the database lacks or has otherwise is its declaration's operation - a field flag's
        ``AlterField``, an entry's ``AddIndex``/``AddConstraint`` (after its removal when it differs); one
        no model declares is removed."""
        owners = self.get_index_owners(model, model_state, field_name_by_column)
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.INDEX, table)
        self.pair_database_named_indexes(table, shadow_only, real_only)
        for part in [*changed, *real_only]:
            owner = owners.get(part.name)
            if owner is not None and owner[0] == "field":
                self.add_field_operation(key, model_state, owner[1], added=False)
                continue
            if (
                owner is not None
                and owner[0] == "constraint"
                or (owner is None and isinstance(part.value, IndexInfo) and part.value.is_unique)
            ):
                self.operations.append(RemoveConstraint(model_name=key[1], name=part.name))
            else:
                self.operations.append(self.get_remove_index_operation(key[1], part))
        for part in [*shadow_only, *changed]:
            owner = owners.get(part.name)
            if owner is None:
                continue
            owner_type, declaration = owner
            if owner_type == "field":
                self.add_field_operation(key, model_state, declaration, added=False)
            elif owner_type == "index":
                self.operations.append(AddIndex(model_name=key[1], index=declaration))
            else:
                self.operations.append(AddConstraint(model_name=key[1], constraint=declaration))

    def pair_database_named_indexes(
        self, table: str, shadow_only: list[SchemaPart], real_only: list[SchemaPart]
    ) -> None:
        """Takes out of the differences an index the database named itself - of a ``UNIQUE`` declared
        without a name - together with the shadow's index of the same definition under the name hare gives
        it: one index, which ``inspectdb`` declares without the database's name.

        Args:
            table: The table.
            shadow_only: The indexes only the shadow has - changed in place.
            real_only: The indexes only the database has - changed in place.
        """
        introspector_class = DatabaseCatalog.get_dialect_introspector_class(self.editor.client.dialect)
        for real_part in list(real_only):
            index = real_part.value
            if not isinstance(index, IndexInfo) or not introspector_class.is_unnamed_index_name(
                index.name, table, index.columns
            ):
                continue
            definition = self.get_definition_without_name(real_part)
            shadow_part = next(
                (part for part in shadow_only if self.get_definition_without_name(part) == definition), None
            )
            if shadow_part is not None:
                real_only.remove(real_part)
                shadow_only.remove(shadow_part)

    @staticmethod
    def get_definition_without_name(part: SchemaPart) -> Any:
        """An index part's definition without its name."""
        if not isinstance(part.definition, dict):
            return part.definition
        return {key: value for key, value in part.definition.items() if key != "name"}

    @staticmethod
    def get_remove_index_operation(model_name: str, part: SchemaPart) -> RemoveIndex:
        """The removal of an index the database has of its own - by its name, or by its columns for one the
        database names itself (a spatial index kept in a table of its own).

        Args:
            model_name: The model of its table.
            part: The index's part.

        Returns:
            The operation.
        """
        if part.name:
            return RemoveIndex(model_name=model_name, name=part.name)
        return RemoveIndex(model_name=model_name, fields=list(part.value.columns))

    def compare_constraints(self, key: tuple[str, str], model: type[Model], table: str) -> None:
        """A check or exclusion constraint the database lacks is added, one it has otherwise removed and
        added, one no model declares removed."""
        declared = {
            constraint.name: constraint
            for constraint in model._meta.constraints
            if isinstance(constraint, (CheckConstraint, ExclusionConstraint, UniqueConstraint))
        }
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.CONSTRAINT, table)
        for part in [*changed, *real_only]:
            self.operations.append(RemoveConstraint(model_name=key[1], name=part.name))
        for part in [*shadow_only, *changed]:
            if part.name in declared:
                self.operations.append(AddConstraint(model_name=key[1], constraint=declared[part.name]))

    def compare_foreign_keys(
        self,
        key: tuple[str, str],
        model: type[Model],
        model_state: ModelState,
        table: str,
        field_name_by_column: dict[str, str],
    ) -> None:
        """A foreign key the database lacks, has otherwise or has of its own is its column's field's
        ``AlterField`` - one declared with ``swappable()`` referencing the table of the model its setting
        pointed at before is a ``ColumnMismatch``: no operation moves the rows."""
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.FOREIGN_KEY, table)
        for part in [*shadow_only, *real_only, *changed]:
            column = part.name.split(",")[0]
            field_name = field_name_by_column.get(column)
            if field_name is None:
                continue
            real_part = self.real_parts.get(part.key)
            detail = SwappableSettingCheck.get_foreign_key_mismatch(
                model._meta.fields_map[field_name], getattr(getattr(real_part, "value", None), "target_table", None)
            )
            if detail is not None:
                self.mismatched_columns.append(ColumnMismatch(key[0], key[1], model_state.table, column, detail))
                continue
            self.add_field_operation(key, model_state, field_name, added=False)

    def compare_named_objects(
        self, part_type: SchemaPartType, model: type[Model], model_name: str, table: str, name_prefix: str = ""
    ) -> None:
        """A named object of a model - a trigger, a policy, a sequence, a view, a function, a dictionary -
        the database lacks is added, one it has otherwise altered, one no model declares removed.

        Args:
            part_type: The objects' part type.
            model: The model, rendered from the declared state.
            model_name: Its name.
            table: The table they belong to - ``""`` for the schema's own objects.
            name_prefix: What names an object of the schema's own among the parts of every schema.
        """
        attribute_name, add_class, remove_class, alter_class = DRIFT_NAMED_OBJECT_OPERATIONS[part_type]
        declared = {
            f"{name_prefix}{declaration.name}": declaration for declaration in getattr(model._meta, attribute_name)
        }
        shadow_only, real_only, changed = self.get_differences(part_type, table)
        for part in shadow_only:
            if part.name in declared:
                self.operations.append(add_class(model_name, declared[part.name]))
        for part in changed:
            if part.name in declared:
                self.operations.append(alter_class(model_name, declared[part.name]))
        if remove_class is not None:
            for part in real_only:
                self.operations.append(remove_class(model_name, part.name.removeprefix(name_prefix)))

    def compare_grants(self, model_name: str, table: str) -> None:
        """A grant the database lacks is added, one it has of its own revoked."""
        shadow_only, real_only, _changed = self.get_differences(SchemaPartType.GRANT, table)
        for part in real_only:
            if not isinstance(part.value, str):
                self.operations.append(RemoveGrant(model_name, part.value))
        for part in shadow_only:
            if not isinstance(part.value, str):
                self.operations.append(AddGrant(model_name, part.value))

    def compare_table_options(self, model_name: str, model_state: ModelState, table: str) -> None:
        """Partitions added or dropped by hand are their ``AddPartition``/``RemovePartition``; other storage
        and the table's comment ``AlterModelOptions``; row level security ``AlterRowLevelSecurity``."""
        key = (SchemaPartType.TABLE_OPTIONS, table, table)
        shadow_part, real_part = self.shadow_parts.get(key), self.real_parts.get(key)
        if shadow_part is not None and real_part is not None and shadow_part.definition != real_part.definition:
            declared_options, real_options = shadow_part.value, real_part.value
            if (
                declared_options is not None
                and real_options is not None
                and real_options.can_change_partitions_to(declared_options)
                and real_options.with_partitions(declared_options.get_partitions()) == declared_options
            ):
                declared_partitions, real_partitions = declared_options.get_partitions(), real_options.get_partitions()
                for name, partition in real_partitions.items():
                    if declared_partitions.get(name) != partition:
                        self.operations.append(RemovePartition(model_name=model_name, partition=partition))
                for name, partition in declared_partitions.items():
                    if real_partitions.get(name) != partition:
                        self.operations.append(AddPartition(model_name=model_name, partition=partition))
            else:
                self.operations.append(AlterModelOptions(name=model_name, options=dict(model_state.options)))
        # A partitioning hare declares no such one of, or another comment - the options aren't the table's.
        for options_key in (
            (SchemaPartType.UNPARSED_PARTITIONING, table, table),
            (SchemaPartType.COMMENT, table, table),
        ):
            if options_key in self.shadow_parts and options_key in self.real_parts:
                if self.shadow_parts[options_key].definition != self.real_parts[options_key].definition:
                    self.operations.append(AlterModelOptions(name=model_name, options=dict(model_state.options)))
        security_key = (SchemaPartType.ROW_LEVEL_SECURITY, table, table)
        if security_key in self.shadow_parts and security_key in self.real_parts:
            if self.shadow_parts[security_key].definition != self.real_parts[security_key].definition:
                self.operations.append(
                    AlterRowLevelSecurity(
                        model_name=model_name, row_level_security=self.shadow_parts[security_key].value
                    )
                )

    def compare_through_tables(self, key: tuple[str, str], model_state: ModelState, prefix: str = "") -> None:
        """A many-to-many relation's automatic through table the database lacks - or has without one of its
        key columns or its unique key - is the relation's ``AddField``; one it has otherwise - an index, a
        foreign key - its ``AlterField``."""
        model = self.new_state.apps.get_model(f"{key[0]}.{key[1]}")
        for field_name in model_state.fields:
            field = model._meta.fields_map.get(field_name)
            if not isinstance(field, ManyToManyFieldInstance) or field._generated or field.through_model is not None:
                continue
            through_table = f"{prefix}{field.through}"
            if not self.has_table(through_table):
                self.add_field_operation(key, model_state, field_name, added=True)
                continue
            if isinstance(field.model_name, SwappableModelReference):
                foreign_key_targets = {
                    part.name.split(",")[0]: part.value.target_table
                    for part in self.real_parts_by_table.get((SchemaPartType.FOREIGN_KEY, through_table), {}).values()
                    if hasattr(part.value, "target_table")
                }
                swappable_mismatches = SwappableSettingCheck.get_through_table_mismatches(
                    model, field, foreign_key_targets
                )
                if swappable_mismatches:
                    # The table of the model the setting pointed at before - no operation moves the rows.
                    self.mismatched_columns.extend(
                        ColumnMismatch(key[0], key[1], through_table, column, detail)
                        for column, detail in swappable_mismatches
                    )
                    continue
            column_differences = [
                part for parts in self.get_differences(SchemaPartType.COLUMN, through_table) for part in parts
            ]
            unique_key_differences = [
                part
                for parts in self.get_differences(SchemaPartType.INDEX, through_table)
                for part in parts
                if isinstance(part.value, IndexInfo) and part.value.is_unique
            ]
            if column_differences or unique_key_differences:
                self.add_field_operation(key, model_state, field_name, added=True)
                continue
            for part_type in SchemaPartType:
                shadow_only, real_only, changed = self.get_differences(part_type, through_table)
                if shadow_only or real_only or changed:
                    self.add_field_operation(key, model_state, field_name, added=False)
                    break

    def compare_schema_objects(self, key: tuple[str, str], prefix: str = "") -> None:
        """Compares the views, materialized views, functions and dictionaries a model declares, and the
        grants on its sequences and functions.

        Args:
            key: The model's (app, name).
            prefix: What names an object of the model's schema among the parts of every schema.
        """
        model = self.new_state.apps.get_model(f"{key[0]}.{key[1]}")
        for part_type in DRIFT_SCHEMA_OBJECT_PART_TYPES:
            self.compare_named_objects(part_type, model, key[1], "", prefix)
        self.compare_object_grants(model, key[1])

    def compare_object_grants(self, model: type[Model], model_name: str) -> None:
        """A grant on a sequence or a function of a model the database lacks is added, one it has of its own
        revoked.

        Args:
            model: The model.
            model_name: Its name.
        """
        object_names = {
            *(sequence.name for sequence in model._meta.sequences),
            *(function.name for function in model._meta.functions),
        }
        shadow_only, real_only, _changed = self.get_differences(SchemaPartType.GRANT, "")
        for part in real_only:
            if isinstance(part.value, Grant) and part.value.object_name in object_names:
                self.operations.append(RemoveGrant(model_name, part.value))
        for part in shadow_only:
            if isinstance(part.value, Grant) and part.value.object_name in object_names:
                self.operations.append(AddGrant(model_name, part.value))

    def compare_enum_types(self) -> None:
        """An ``ENUM`` type the database lacks is created, one of other labels altered."""
        shadow_only, _real_only, changed = self.get_differences(SchemaPartType.ENUM_TYPE, "")
        for part in shadow_only:
            enum_type = part.value
            self.operations.append(CreateEnumType(enum_type.name, enum_type.labels, enum_type.schema))
        for part in changed:
            real_type = self.real_parts[part.key].value
            self.operations.append(
                AlterEnumType(part.value.name, real_type.labels, part.value.labels, part.value.schema)
            )
