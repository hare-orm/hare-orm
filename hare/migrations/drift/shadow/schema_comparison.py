from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hare.core.apps.relation_linking import RelationLinking
from hare.core.caching.cache import Cache
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GrantTarget, SchemaPartType
from hare.ddl.indexes.index import Index
from hare.ddl.security.grant import Grant
from hare.fields.constants import DB_DEFAULT_NOT_SET
from hare.fields.field import Field
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.inspectdb.generation.inspected_column_defaults import InspectedColumnDefaults
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.autodetection.diffs.partition_diff import PartitionDiff
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.constants import MODEL_OPTIONS_WITH_DEDICATED_OPERATIONS
from hare.migrations.drift.column_mismatch import ColumnMismatch
from hare.migrations.drift.constants import DRIFT_NAMED_OBJECT_OPERATIONS, DRIFT_SCHEMA_OBJECT_PART_TYPES
from hare.migrations.drift.messages import (
    MISSING_SECONDARY_TABLE,
    UNREPRESENTABLE_COLUMN,
    UNREPRESENTABLE_FUNCTION,
    UNREPRESENTABLE_GRANT,
    UNREPRESENTABLE_PARTITIONING,
    UNREPRESENTABLE_PRIMARY_KEY,
    UNREPRESENTABLE_SCHEMA_OBJECT,
    UNTRACKED_COLUMNS_BLOCK_REPAIR,
)
from hare.migrations.drift.object_mismatch import ObjectMismatch
from hare.migrations.drift.shadow.column_field_names import ColumnFieldNames
from hare.migrations.drift.shadow.observed_table import ObservedTable
from hare.migrations.execution.swappable_setting_check import SwappableSettingCheck
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddGrant,
    AddIndex,
    AlterEnumType,
    AlterField,
    AlterModelOptions,
    AlterRowLevelSecurity,
    CreateEnumType,
    HareOperation,
    Operation,
    RemoveConstraint,
    RemoveField,
    RemoveGrant,
    RemoveIndex,
    SeparateDatabaseAndState,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models.model_binding import ModelBinding


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

    observed_tables = Cache[ObservedTable]()

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
        self.observed_state: list[Operation] = []
        self.untracked_columns: list[tuple[str, str, str, str]] = []
        self.mismatched_columns: list[ColumnMismatch] = []
        self.mismatched_objects: list[ObjectMismatch] = []
        # One operation per field: adding it outweighs altering it.
        self.field_operations: dict[tuple[str, str, str], tuple[HareOperation, HareOperation]] = {}

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
        real_only = [part for key, part in real_parts.items() if key not in shadow_parts]
        shadow_only = []
        changed = []
        for key, part in shadow_parts.items():
            actual = real_parts.get(key)
            if actual is None:
                shadow_only.append(part)
            elif part.definition != actual.definition:
                changed.append(part)
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
        binding = self.new_state.apps.get_binding(f"{app_label}.{model_name}")
        tables = [f"{prefix}{table}" for table in tables]
        if not self.has_table(tables[0]):
            self.operations.append(OperationGenerator.get_create_model_operation(model_state))
            return
        operation_start, state_start = len(self.operations), len(self.observed_state)
        untracked_start = len(self.untracked_columns)
        field_name_by_column = ColumnFieldNames.column_to_field_name(binding, model_state.fields)
        for table in tables:
            if not self.has_table(table):
                for part in self.shadow_parts_by_table.get((SchemaPartType.TABLE, table), {}).values():
                    self.add_object_mismatch(part, MISSING_SECONDARY_TABLE)
                continue
            self.compare_columns(key, model_state, table, field_name_by_column)
            self.compare_primary_key(key, model_state, table)
            self.compare_indexes(
                key,
                binding,
                model_state,
                table,
                field_name_by_column,
            )
            self.compare_constraints(key, binding, model_state, table)
            self.compare_foreign_keys(
                key,
                binding,
                model_state,
                table,
                field_name_by_column,
            )
            for part_type in (SchemaPartType.TRIGGER, SchemaPartType.POLICY):
                self.compare_named_objects(part_type, binding, model_name, table)
            self.compare_grants(model_name, table)
            self.compare_table_options(model_name, model_state, table)
        self.compare_through_tables(key, binding, model_state, prefix)
        if len(self.untracked_columns) > untracked_start and len(self.operations) > operation_start:
            del self.operations[operation_start:]
            del self.observed_state[state_start:]
            self.add_object_mismatch(
                SchemaPart(SchemaPartType.TABLE, tables[0], model_state.table, None), UNTRACKED_COLUMNS_BLOCK_REPAIR
            )
        self.field_operations.clear()

    def add_field_operation(
        self,
        key: tuple[str, str],
        model_state: ModelState,
        field_name: str,
        table: str,
        column: str,
        *,
        added: bool = False,
        previous: Field[Any] | None = None,
    ) -> None:
        """Records the operation of one of a model's fields - its addition, else its alteration.

        Args:
            key: The model's (app, name).
            model_state: Its declared state.
            field_name: The field.
            table: The table read from the catalog.
            column: The physical column naming the field.
            added: Whether its column is missing.
            previous: An observed declaration already established from the catalog.
        """
        operation_key = (key[0], key[1], field_name)
        existing = self.field_operations.get(operation_key)
        if existing is not None and (isinstance(existing[-1], AddField) or (not added and previous is None)):
            return
        field = model_state.fields[field_name]
        state_operation: HareOperation
        if added:
            state_operation = RemoveField(key[1], field_name)
            operation: HareOperation = AddField(key[1], field_name, field)
        else:
            if previous is None:
                observed = self.get_observed_table(table, model_state)
                previous = None if observed is None else observed.get_field(column, field_name)
            if previous is None:
                self.add_object_mismatch(
                    SchemaPart(SchemaPartType.COLUMN, table, column, None), UNREPRESENTABLE_COLUMN
                )
                return
            state_operation = AlterField(key[1], field_name, previous)
            operation = AlterField(key[1], field_name, field)
        if existing is not None:
            self.observed_state[self.observed_state.index(existing[0])] = state_operation
            self.operations[self.operations.index(existing[-1])] = operation
        else:
            self.add_repair(operation, state_operation)
        self.field_operations[operation_key] = (state_operation, operation)

    def get_observed_table(self, table: str, model_state: ModelState) -> ObservedTable | None:
        """Use the declaration mapper on the table already read by the catalog comparison."""
        part = next(iter(self.real_parts_by_table.get((SchemaPartType.TABLE, table), {}).values()), None)
        if part is None or not isinstance(part.value, TableInfo):
            return None
        bucket = self.observed_tables.get_bucket(self)
        key = (table,)
        observed = bucket.get(key)
        if observed is None:
            observed = ObservedTable(part.value, self.editor.client.dialect, model_state, self.new_state)
            bucket[key] = observed
        return observed

    def add_repair(self, operation: HareOperation, *observed_state: HareOperation) -> None:
        """Describe the physical starting point before giving the database operation."""
        self.observed_state.extend(observed_state)
        self.operations.append(operation)

    def take_repair_operations(self) -> list[HareOperation]:
        """Start from all observed definitions before a DDL operation can rebuild their table."""
        operations, self.operations = self.operations, []
        observed_state, self.observed_state = self.observed_state, []
        self.field_operations.clear()
        if observed_state:
            observed_state.sort(key=lambda operation: isinstance(operation, RemoveField))
            operations.insert(0, SeparateDatabaseAndState(state_operations=observed_state))
        return operations

    def compare_columns(
        self, key: tuple[str, str], model_state: ModelState, table: str, field_name_by_column: dict[str, str]
    ) -> None:
        """A column the database lacks is its field's ``AddField``, one read otherwise its ``AlterField``,
        one the model has no field for an untracked column."""
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.COLUMN, table)
        for part in shadow_only:
            self.add_field_operation(key, model_state, field_name_by_column[part.name], table, part.name, added=True)
        for part in changed:
            real_part = self.real_parts[part.key]
            if part.unrepresented_definition != real_part.unrepresented_definition:
                self.add_object_mismatch(real_part, UNREPRESENTABLE_COLUMN)
            if SchemaPart.get_definition(part.value) == SchemaPart.get_definition(real_part.value):
                self.add_object_mismatch(real_part, UNREPRESENTABLE_COLUMN)
                continue
            field_name = field_name_by_column[part.name]
            previous = None
            if (
                isinstance(real_part.value, ColumnInfo)
                and isinstance(part.definition, dict)
                and isinstance(real_part.definition, dict)
                and part.definition.keys() == real_part.definition.keys()
                and {name for name, value in part.definition.items() if value != real_part.definition[name]}
                == {"column_default"}
                and not any(self.get_differences(SchemaPartType.FOREIGN_KEY, table))
            ):
                previous = model_state.fields[field_name].snapshot()
                if real_part.value.db_default is None:
                    previous.db_default = DB_DEFAULT_NOT_SET
                else:
                    dialect = self.editor.client.dialect
                    path, _, _ = previous.deconstruct()
                    default = InspectedColumnDefaults.get_for_path(dialect, path, real_part.value.db_default)
                    previous.db_default = InspectedColumnDefaults.get_for_field(dialect, previous, default)
            self.add_field_operation(key, model_state, field_name, table, part.name, previous=previous)
        for part in real_only:
            if part.name in field_name_by_column:
                # A column of a field the shadow lays out otherwise - its field is altered.
                self.add_field_operation(
                    key, model_state, field_name_by_column[part.name], table, part.name, added=False
                )
            else:
                self.untracked_columns.append((key[0], key[1], model_state.table, part.name))

    def compare_primary_key(self, key: tuple[str, str], model_state: ModelState, table: str) -> None:
        """A primary key of other columns is a ``ColumnMismatch`` of each - no operation describes it."""
        _, _, changed = self.get_differences(SchemaPartType.PRIMARY_KEY, table)
        if not changed:
            return
        [shadow_part] = changed
        real_part = self.real_parts[shadow_part.key]
        declared_columns = shadow_part.value["columns"]
        real_columns = real_part.value["columns"]
        differing_columns = [column for column in declared_columns if column not in real_columns] + [
            column for column in real_columns if column not in declared_columns
        ]
        if not differing_columns and not declared_columns:
            self.add_object_mismatch(real_part, UNREPRESENTABLE_PRIMARY_KEY)
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
        self, binding: ModelBinding, model_state: ModelState, field_name_by_column: dict[str, str]
    ) -> dict[str, tuple[str, Any]]:
        """What declares each index and unique constraint of a model's table, by its name: a field's flag
        (``("field", field name)``), a ``Meta.indexes`` entry (``("index", Index)``) or a
        ``Meta.constraints`` one (``("constraint", constraint)``).

        Args:
            binding: The model, rendered from the declared state.
            model_state: Its declared state.
            field_name_by_column: The field of the declared state owning each column.

        Returns:
            The owners by name.
        """
        owners: dict[str, tuple[str, Any]] = {}

        for (owner_type, owner_id), (
            index,
            _is_constraint,
        ) in self.editor.generated_index_names.get_generated_index_names(binding).items():
            if owner_type in ("field", "unique"):
                owners[str(index.name)] = (
                    "field",
                    self.get_state_field_name(binding, model_state, field_name_by_column, owner_id),
                )
            elif owner_type == "index":
                entry = binding.indexes[owner_id]
                owners[str(index.name)] = ("index", Index.from_declaration(entry))
            elif owner_type == "constraint":
                constraint = binding.constraints[owner_id]
                relation_name = RelationLinking.get_unique_relation_name(binding, constraint)
                if relation_name is not None:
                    owners[str(index.name)] = ("field", relation_name)
                else:
                    owners[str(index.name)] = ("constraint", constraint)
        for entry in binding.indexes:
            if isinstance(entry, Index) and entry.name:
                owners[entry.name] = ("index", entry)
        for constraint in binding.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.name:
                owners[constraint.name] = ("constraint", constraint)
        return owners

    @staticmethod
    def get_state_field_name(
        binding: ModelBinding, model_state: ModelState, field_name_by_column: dict[str, str], field_name: str
    ) -> str:
        """The field of the declared state a field of the rendered model is - a relation's key field
        (``author_id``) is the relation (``author``).

        Args:
            binding: The model, rendered from the declared state.
            model_state: Its declared state.
            field_name_by_column: The field of the declared state owning each column.
            field_name: The rendered model's field.

        Returns:
            The declared state's field.
        """
        if field_name in model_state.fields:
            return field_name
        column = binding.fields_db_projection.get(field_name, field_name)
        return field_name_by_column.get(column, field_name)

    def compare_indexes(
        self,
        key: tuple[str, str],
        binding: ModelBinding,
        model_state: ModelState,
        table: str,
        field_name_by_column: dict[str, str],
    ) -> None:
        """An index the database lacks or has otherwise is its declaration's operation - a field flag's
        ``AlterField``, an entry's ``AddIndex``/``AddConstraint`` (after its removal when it differs); one
        no model declares is removed."""
        owners = self.get_index_owners(binding, model_state, field_name_by_column)
        column_by_field = {field: column for column, field in field_name_by_column.items()}
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.INDEX, table)
        self.pair_database_named_indexes(table, shadow_only, real_only)
        removed_names = set()
        for part in [*changed, *real_only]:
            owner = owners.get(part.name)
            if owner is not None and owner[0] == "field":
                self.add_field_operation(key, model_state, owner[1], table, column_by_field[owner[1]], added=False)
                continue
            observed = self.get_observed_table(table, model_state)
            unique_constraint = (
                owner is not None
                and owner[0] == "constraint"
                or (owner is None and isinstance(part.value, IndexInfo) and part.value.is_unique)
            )
            remove: HareOperation
            forget_declared: HareOperation
            prepare: HareOperation | None
            if unique_constraint:
                previous = observed.get_named_object(ModelOption.CONSTRAINTS, part.name) if observed else None
                remove = RemoveConstraint(model_name=key[1], name=part.name)
                prepare = AddConstraint(model_name=key[1], constraint=previous) if previous is not None else None
                forget_declared = remove
            else:
                previous = observed.get_named_object(ModelOption.INDEXES, part.name) if observed else None
                remove = self.get_remove_index_operation(key[1], part)
                prepare = AddIndex(model_name=key[1], index=previous) if previous is not None else None
                forget_declared = RemoveIndex(model_name=key[1], index=owner[1]) if owner is not None else remove
            if prepare is None:
                self.add_object_mismatch(part, UNREPRESENTABLE_SCHEMA_OBJECT)
                continue
            self.add_repair(remove, *((forget_declared,) if owner is not None else ()), prepare)
            removed_names.add(part.name)
        for part in [*shadow_only, *changed]:
            owner = owners.get(part.name)
            if owner is None:
                continue
            owner_type, declaration = owner
            operation: HareOperation
            if owner_type == "field":
                field = model_state.fields[declaration]
                index = part.value
                previous = None
                if part in shadow_only and field.index and isinstance(index, IndexInfo) and not index.is_unique:
                    existing = self.field_operations.get((*key, declaration))
                    if existing is not None and isinstance(existing[0], AlterField):
                        observed_field = existing[0].field
                        if isinstance(observed_field, Field):
                            previous = observed_field.snapshot()
                    elif all(
                        self.real_parts.get((SchemaPartType.COLUMN, table, column))
                        == self.shadow_parts.get((SchemaPartType.COLUMN, table, column))
                        for column in index.columns
                    ) and not any(self.get_differences(SchemaPartType.FOREIGN_KEY, table)):
                        previous = field.snapshot()
                    if previous is not None:
                        previous.index = False
                self.add_field_operation(
                    key, model_state, declaration, table, column_by_field[declaration], previous=previous
                )
            elif owner_type == "index":
                operation = AddIndex(model_name=key[1], index=declaration)
                if part in shadow_only:
                    self.add_repair(operation, RemoveIndex(model_name=key[1], index=declaration))
                elif part.name in removed_names:
                    self.operations.append(operation)
            else:
                operation = AddConstraint(model_name=key[1], constraint=declaration)
                if part in shadow_only:
                    self.add_repair(operation, RemoveConstraint(model_name=key[1], name=part.name))
                elif part.name in removed_names:
                    self.operations.append(operation)

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

    def compare_constraints(
        self, key: tuple[str, str], binding: ModelBinding, model_state: ModelState, table: str
    ) -> None:
        """A check or exclusion constraint the database lacks is added, one it has otherwise removed and
        added, one no model declares removed."""
        declared = {
            constraint.name: constraint
            for constraint in binding.constraints
            if isinstance(constraint, (CheckConstraint, ExclusionConstraint, UniqueConstraint))
        }
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.CONSTRAINT, table)
        for part in shadow_only:
            if part.name in declared:
                self.add_repair(
                    AddConstraint(model_name=key[1], constraint=declared[part.name]),
                    RemoveConstraint(model_name=key[1], name=part.name),
                )
        if changed or real_only:
            observed = self.get_observed_table(table, model_state)
            for part in [*changed, *real_only]:
                previous = observed.get_named_object(ModelOption.CONSTRAINTS, part.name) if observed else None
                if previous is None:
                    self.add_object_mismatch(part, UNREPRESENTABLE_SCHEMA_OBJECT)
                    continue
                remove = RemoveConstraint(model_name=key[1], name=part.name)
                prepare = AddConstraint(model_name=key[1], constraint=previous)
                if part.name in declared:
                    self.add_repair(remove, remove, prepare)
                    self.operations.append(AddConstraint(model_name=key[1], constraint=declared[part.name]))
                else:
                    self.add_repair(remove, prepare)

    def compare_foreign_keys(
        self,
        key: tuple[str, str],
        binding: ModelBinding,
        model_state: ModelState,
        table: str,
        field_name_by_column: dict[str, str],
    ) -> None:
        """A foreign key the database lacks, has otherwise or has of its own is its column's field's
        ``AlterField`` - one declared with ``swappable()`` referencing the table of the model its setting
        pointed at before is a ``ColumnMismatch``: no operation moves the rows."""
        shadow_only, real_only, changed = self.get_differences(SchemaPartType.FOREIGN_KEY, table)
        for part in [*shadow_only, *real_only, *changed]:
            if isinstance(part.value, ForeignKeyInfo):
                column = part.value.column
            elif isinstance(part.value, CompositeForeignKeyInfo):
                column = part.value.columns[0]
            else:
                # An unrepresentable constraint retains the catalog diagnostic's name.
                column = part.name.split(",")[0]
            field_name = field_name_by_column.get(column)
            if field_name is None:
                continue
            real_part = self.real_parts.get(part.key)
            detail = SwappableSettingCheck.get_foreign_key_mismatch(
                binding.fields_map[field_name], getattr(getattr(real_part, "value", None), "target_table", None)
            )
            if detail is not None:
                self.mismatched_columns.append(ColumnMismatch(key[0], key[1], model_state.table, column, detail))
                continue
            self.add_field_operation(key, model_state, field_name, table, column, added=False)

    def compare_named_objects(
        self, part_type: SchemaPartType, binding: ModelBinding, model_name: str, table: str, name_prefix: str = ""
    ) -> None:
        """A named object of a model - a trigger, a policy, a sequence, a view, a function, a dictionary -
        the database lacks is added, one it has otherwise altered, one no model declares removed.

        Args:
            part_type: The objects' part type.
            binding: The model, rendered from the declared state.
            model_name: Its name.
            table: The table they belong to - ``""`` for the schema's own objects.
            name_prefix: What names an object of the schema's own among the parts of every schema.
        """
        attribute_name, add_class, remove_class, alter_class = DRIFT_NAMED_OBJECT_OPERATIONS[part_type]
        declared = {
            f"{name_prefix}{declaration.name}": declaration for declaration in getattr(binding, attribute_name)
        }
        shadow_only, real_only, changed = self.get_differences(part_type, table)
        for part in shadow_only:
            if part.name in declared:
                declaration = declared[part.name]
                self.add_repair(add_class(model_name, declaration), remove_class(model_name, declaration.name))
        for part in changed:
            if part.name in declared:
                real = self.real_parts[part.key]
                if part_type is SchemaPartType.FUNCTION and not self._has_same_single_operation_signature(real, part):
                    self.add_object_mismatch(real, UNREPRESENTABLE_FUNCTION)
                    continue
                previous = real.value
                if part_type is SchemaPartType.SEQUENCE:
                    owned_by = previous.owned_by
                    owner_table = real.definition["owner_table"]
                    if owner_table is not None and owner_table != binding.db_table:
                        self.add_object_mismatch(real, UNREPRESENTABLE_SCHEMA_OBJECT)
                        continue
                    previous = replace(previous, owned_by=binding.fields_db_projection_reverse.get(owned_by, owned_by))
                if previous is None:
                    self.add_object_mismatch(real, UNREPRESENTABLE_SCHEMA_OBJECT)
                    continue
                self.add_repair(alter_class(model_name, declared[part.name]), alter_class(model_name, previous))
        for part in real_only:
            if not table:
                continue
            if part.value is None or not hasattr(part.value, "deconstruct"):
                self.add_object_mismatch(part, UNREPRESENTABLE_SCHEMA_OBJECT)
            else:
                self.add_repair(remove_class(model_name, part.name), add_class(model_name, part.value))

    def _get_grant_changes(self, table: str) -> Iterator[tuple[SchemaPart, type[AddGrant] | type[RemoveGrant]]]:
        """Revoke unexpected grants before adding missing ones."""
        shadow_only, real_only, _changed = self.get_differences(SchemaPartType.GRANT, table)
        for part in real_only:
            yield part, RemoveGrant
        for part in shadow_only:
            yield part, AddGrant

    def compare_grants(self, model_name: str, table: str) -> None:
        """A grant the database lacks is added, one it has of its own revoked."""
        for part, operation in self._get_grant_changes(table):
            if isinstance(part.value, Grant):
                self.operations.append(operation(model_name, part.value))
            else:
                self.add_object_mismatch(part, UNREPRESENTABLE_GRANT)

    def add_object_mismatch(self, part: SchemaPart, detail: str) -> None:
        """Keep unsupported differences visible without inventing a corrective operation."""
        mismatch = ObjectMismatch(part.part_type, part.table, part.name, detail)
        if mismatch not in self.mismatched_objects:
            self.mismatched_objects.append(mismatch)

    def compare_table_options(self, model_name: str, model_state: ModelState, table: str) -> None:
        """Partitions added or dropped by hand are their ``AddPartition``/``RemovePartition``; other storage
        and the table's comment ``AlterModelOptions``; row level security ``AlterRowLevelSecurity``."""
        table_name = model_state.db_table
        key = (SchemaPartType.TABLE_OPTIONS, table, table_name)
        changes_options = False
        shadow_part, real_part = self.shadow_parts.get(key), self.real_parts.get(key)
        storage_change = (
            (shadow_part, real_part)
            if shadow_part is not None and real_part is not None and shadow_part.definition != real_part.definition
            else None
        )
        partitioning_key = (SchemaPartType.UNPARSED_PARTITIONING, table, table_name)
        shadow_partitioning = self.shadow_parts.get(partitioning_key)
        real_partitioning = self.real_parts.get(partitioning_key)
        opaque = next((part for part in (real_partitioning, shadow_partitioning) if part and part.value), None)
        if opaque is not None:
            if storage_change is not None or (shadow_partitioning and shadow_partitioning.definition) != (
                real_partitioning and real_partitioning.definition
            ):
                self.add_object_mismatch(opaque, UNREPRESENTABLE_PARTITIONING)
        elif storage_change is not None:
            expected, observed = storage_change
            declared_options, real_options = expected.value, observed.value
            if (
                declared_options is not None
                and real_options is not None
                and real_options.can_change_partitions_to(declared_options)
                and real_options.with_partitions(declared_options.get_partitions()) == declared_options
            ):
                declared_partitions, real_partitions = declared_options.get_partitions(), real_options.get_partitions()
                changes = PartitionDiff.get_partition_changes(model_name, real_partitions, declared_partitions)
                if changes:
                    self.observed_state.append(self.get_table_options_operation(model_state, real_options))
                    self.operations.extend(changes)
            else:
                changes_options = True
        comment_key = (SchemaPartType.COMMENT, table, table_name)
        shadow_comment = self.shadow_parts.get(comment_key)
        real_comment = self.real_parts.get(comment_key)
        comment_differs = (
            shadow_comment is not None
            and real_comment is not None
            and shadow_comment.definition != real_comment.definition
        )
        if changes_options or comment_differs:
            actual = self.real_parts.get((SchemaPartType.TABLE, table, table_name))
            if actual is not None and isinstance(actual.value, TableInfo):
                options = {
                    name: value
                    for name, value in model_state.options.items()
                    if name not in MODEL_OPTIONS_WITH_DEDICATED_OPERATIONS
                }
                previous = (
                    AlterModelOptions(model_name, dict(options))
                    if opaque is not None
                    else self.get_table_options_operation(model_state, actual.value.table_options)
                )
                previous.options[ModelOption.TABLE_DESCRIPTION] = actual.value.table_description or ""
                self.add_repair(AlterModelOptions(model_name, options), previous)
        security_key = (SchemaPartType.ROW_LEVEL_SECURITY, table, table_name)
        if security_key in self.shadow_parts and security_key in self.real_parts:
            if self.shadow_parts[security_key].definition != self.real_parts[security_key].definition:
                self.add_repair(
                    AlterRowLevelSecurity(
                        model_name=model_name, row_level_security=self.shadow_parts[security_key].value
                    ),
                    AlterRowLevelSecurity(
                        model_name=model_name, row_level_security=self.real_parts[security_key].value
                    ),
                )

    @staticmethod
    def get_table_options_operation(model_state: ModelState, table_options: Any) -> AlterModelOptions:
        """Restore the catalog's storage options before changing a table or its partitions."""
        options = {
            name: value
            for name, value in model_state.options.items()
            if name not in MODEL_OPTIONS_WITH_DEDICATED_OPERATIONS
        }
        options[ModelOption.TABLE_OPTIONS] = (table_options,) if table_options is not None else ()
        return AlterModelOptions(model_state.name, options)

    def compare_through_tables(
        self, key: tuple[str, str], binding: ModelBinding, model_state: ModelState, prefix: str = ""
    ) -> None:
        """Propose creating a missing through table and report differences in one holding links."""
        for field_name in model_state.fields:
            field = binding.fields_map.get(field_name)
            if not isinstance(field, ManyToManyFieldInstance) or field._generated or field.through_model is not None:
                continue
            through_table = f"{prefix}{field.through}"
            if not self.has_table(through_table):
                self.add_field_operation(key, model_state, field_name, through_table, field_name, added=True)
                continue
            if isinstance(field.model_name, SwappableModelReference):
                foreign_key_targets = {
                    (
                        part.value.column if isinstance(part.value, ForeignKeyInfo) else part.value.columns[0]
                    ): part.value.target_table
                    for part in self.real_parts_by_table.get((SchemaPartType.FOREIGN_KEY, through_table), {}).values()
                    if isinstance(part.value, (ForeignKeyInfo, CompositeForeignKeyInfo))
                }
                swappable_mismatches = SwappableSettingCheck.get_through_table_mismatches(
                    binding, field, foreign_key_targets
                )
                if swappable_mismatches:
                    # The table of the model the setting pointed at before - no operation moves the rows.
                    self.mismatched_columns.extend(
                        ColumnMismatch(key[0], key[1], through_table, column, detail)
                        for column, detail in swappable_mismatches
                    )
                    continue
            differences = {
                part_type: parts
                for part_type in SchemaPartType
                if any(parts := self.get_differences(part_type, through_table))
            }
            if not differences:
                continue
            if differences.keys() == {SchemaPartType.INDEX}:
                missing, extra, changed = differences[SchemaPartType.INDEX]
                indexes = self.editor.many_to_many_through_tables.get_through_indexes(field)
                plain_index_names = {name for name, (_columns, unique) in indexes.items() if not unique}
                if missing and not extra and not changed and {part.name for part in missing} == plain_index_names:
                    previous = model_state.fields[field_name].snapshot()
                    previous.index = False
                    self.add_field_operation(
                        key, model_state, field_name, through_table, field_name, previous=previous
                    )
                    continue
            part = next(iter(self.real_parts_by_table[SchemaPartType.TABLE, through_table].values()))
            self.add_object_mismatch(part, UNREPRESENTABLE_SCHEMA_OBJECT)

    def compare_schema_objects(self, key: tuple[str, str], prefix: str = "") -> None:
        """Compares the views, materialized views, functions and dictionaries a model declares, and the
        grants on its sequences and functions.

        Args:
            key: The model's (app, name).
            prefix: What names an object of the model's schema among the parts of every schema.
        """
        binding = self.new_state.apps.get_binding(f"{key[0]}.{key[1]}")
        for part_type in DRIFT_SCHEMA_OBJECT_PART_TYPES:
            self.compare_named_objects(part_type, binding, key[1], "", prefix)
        self.compare_object_grants(binding, key[1], prefix)

    def compare_object_grants(self, binding: ModelBinding, model_name: str, prefix: str = "") -> None:
        """A grant on a sequence or a function of a model the database lacks is added, one it has of its own
        revoked.

        Args:
            binding: The model.
            model_name: Its name.
            prefix: The logical namespace prefix used by the compared object parts.
        """
        object_names = {
            *(sequence.name for sequence in binding.sequences),
            *(function.name for function in binding.functions),
        }
        for part, operation in self._get_grant_changes(""):
            object_name = (
                part.value.object_name
                if isinstance(part.value, Grant)
                else part.definition.get("object_name")
                if isinstance(part.definition, dict)
                else None
            )
            if object_name not in object_names or part.name != prefix + repr(part.definition):
                continue
            if not isinstance(part.value, Grant) or not self.can_map_function_grant(part.value, prefix):
                self.add_object_mismatch(part, UNREPRESENTABLE_GRANT)
            else:
                self.operations.append(operation(model_name, part.value))

    def can_map_function_grant(self, grant: Grant, prefix: str) -> bool:
        """A name-based Grant cannot choose between different physical overload signatures."""
        if grant.on != GrantTarget.FUNCTION:
            return True
        key = SchemaPartType.FUNCTION, "", prefix + str(grant.object_name)
        real, shadow = self.real_parts.get(key), self.shadow_parts.get(key)
        if real is None or shadow is None:
            return True
        return self._has_same_single_operation_signature(real, shadow)

    @staticmethod
    def _has_same_single_operation_signature(first: SchemaPart, second: SchemaPart) -> bool:
        signatures = first.operation_signatures
        return signatures is not None and len(signatures) == 1 and signatures == second.operation_signatures

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
