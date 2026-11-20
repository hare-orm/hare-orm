from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.partition_changes import PartitionChanges
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.tables.table_partitions import TablePartitions
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.dialects.postgresql.partitioning.partitions.partition import Partition
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_PARTITION_CREATE_TEMPLATE,
    POSTGRESQL_SET_DEFAULT_TABLESPACE_BODY,
)
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.postgresql.declarations import PostgresqlFeatures
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.models.model_binding import ModelBinding


class PostgresqlTablePartitions(TablePartitions):
    """TablePartitions as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_alter_table_options_plan(self, old_binding: ModelBinding, new_binding: ModelBinding) -> SchemaChangePlan:
        """Compile partition and storage changes from the complete before/after models.

        Changing the partitioning strategy uses the same rebuild plan as execution
        and preview. In-place changes are fully rendered before any partition is
        detached, including bounds and options of partitions added later.
        """
        dialect = self.editor.client.dialect
        old: PostgresqlTableOptions = old_binding.get_table_options(dialect) or PostgresqlTableOptions()  # type: ignore[assignment]
        new: PostgresqlTableOptions = new_binding.get_table_options(dialect) or PostgresqlTableOptions()  # type: ignore[assignment]
        new.raise_if_unsupported(new_binding, self.editor.client.features)
        if old.with_partitions({}).partitioning != new.with_partitions({}).partitioning:
            return self.editor.table_rebuild.get_rebuild_plan(old_binding, new_binding)
        changes = PartitionChanges.compare(old.get_partitions(), new.get_partitions())
        steps: list[SchemaStep] = []
        for partition in changes.removed:
            steps.extend(self.get_remove_partition_plan(old_binding, partition).steps)
        for partition, replacement in changes.renamed:
            steps.extend(self.get_rename_partition_plan(old_binding, partition, replacement).steps)
        qualified_table = self.editor.qualify_table_name(new_binding.db_table, new_binding.schema)
        added_partition_names = {partition.name for partition in changes.added}
        stored_tables = self.get_stored_table_names(new_binding, new, skipped_partition_names=added_partition_names)
        if old.tablespace != new.tablespace:
            for table in dict.fromkeys([qualified_table, *stored_tables]):
                steps.append(SchemaStatement(self.get_tablespace_sql(table, new.tablespace)))
        if old.unlogged != new.unlogged:
            steps.append(
                SchemaStatement(f"ALTER TABLE {qualified_table} SET {'UNLOGGED' if new.unlogged else 'LOGGED'}")
            )
        removed_parameters = [name for name in old.storage_parameters if name not in new.storage_parameters]
        changed_parameters = {
            name: value
            for name, value in new.storage_parameters.items()
            if name not in old.storage_parameters or old.storage_parameters[name] != value
        }
        for stored_table in stored_tables:
            if removed_parameters:
                steps.append(SchemaStatement(f"ALTER TABLE {stored_table} RESET ({', '.join(removed_parameters)})"))
            if changed_parameters:
                steps.append(
                    SchemaStatement(
                        f"ALTER TABLE {stored_table} "
                        f"SET ({new.get_storage_parameters_sql(changed_parameters, self.editor)})"
                    )
                )
        for partition in changes.added:
            steps.extend(self.get_add_partition_plan(new_binding, partition).steps)
        return SchemaChangePlan(tuple(steps))

    def get_partition_create_sqls(self, binding: ModelBinding, safe: bool) -> list[str]:
        model = binding._model
        options = self.editor.get_partitioned_options(binding)
        if options is None:
            return []
        features: PostgresqlFeatures = self.editor.client.features  # type: ignore[assignment]
        if not features.supports_partitioned_exclusion_constraints and any(
            isinstance(constraint, ExclusionConstraint) for constraint in binding.constraints
        ):
            raise UnSupportedError(
                f"{model.__name__}: a partitioned table takes an exclusion constraint from PostgreSQL 17 on"
            )
        partitioning: Partitioning = options.partitioning  # type: ignore[assignment]
        bound_sqls = partitioning.get_partition_bound_sqls(self.get_bound_value_renderer(binding, partitioning))
        locations = partitioning.get_partition_locations(binding.db_table, binding.schema)
        return [
            self.get_partition_create_sql(binding, options, locations[partition_name], bound_sql, safe)
            for partition_name, bound_sql in bound_sqls.items()
        ]

    def get_partition_locations(self, binding: ModelBinding) -> dict[str, tuple[str | None, str]]:
        options = self.editor.get_partitioned_options(binding)
        if options is None or options.partitioning is None:
            return {}
        return options.partitioning.get_partition_locations(binding.db_table, binding.schema)

    async def add_partition(self, binding: ModelBinding, partition: Partition) -> None:
        await self.get_add_partition_plan(binding, partition).execute(self.editor)

    def get_add_partition_plan(self, binding: ModelBinding, partition: Partition) -> SchemaChangePlan:
        """Creates a partition of a model's partitioned table.

        Args:
            binding: The model rendered with the partition.
            partition: The partition.

        Raises:
            ConfigurationError: The model's table isn't partitioned.
        """
        model = binding._model
        options = self.editor.get_partitioned_options(binding)
        if options is None:
            raise ConfigurationError(f"{model.__name__}: a partition can only be added to a partitioned table")
        partitioning: Partitioning = options.partitioning  # type: ignore[assignment]
        bound_sql = partition.get_bound_sql(self.get_bound_value_renderer(binding, partitioning))
        location = partition.get_location(binding.db_table, binding.schema)
        sql = self.get_partition_create_sql(binding, options, location, bound_sql, safe=False)
        return SchemaChangePlan((SchemaStatement(sql),))

    async def remove_partition(self, binding: ModelBinding, partition: Partition) -> None:
        await self.get_remove_partition_plan(binding, partition).execute(self.editor)

    def get_remove_partition_plan(self, binding: ModelBinding, partition: Partition) -> SchemaChangePlan:
        """Detaches a partition from a model's table and drops it - with its rows.

        Args:
            binding: The model.
            partition: The partition.
        """
        partition_schema, partition_table = partition.get_location(binding.db_table, binding.schema)
        qualified_partition = self.editor.qualify_table_name(partition_table, partition_schema)
        qualified_table = self.editor.qualify_table_name(binding.db_table, binding.schema)
        return SchemaChangePlan(
            (
                SchemaStatement(f"ALTER TABLE {qualified_table} DETACH PARTITION {qualified_partition}"),
                SchemaStatement(f"DROP TABLE {qualified_partition}"),
            )
        )

    async def rename_partition(self, binding: ModelBinding, partition: Partition, replacement: Partition) -> None:
        await self.get_rename_partition_plan(binding, partition, replacement).execute(self.editor)

    def get_rename_partition_plan(
        self, binding: ModelBinding, partition: Partition, replacement: Partition
    ) -> SchemaChangePlan:
        """Rename or move an attached table, preserving its rows and partition membership."""
        old_schema, old_table = partition.get_location(binding.db_table, binding.schema)
        new_schema, new_table = replacement.get_location(binding.db_table, binding.schema)
        steps: list[SchemaStep] = []
        if old_schema != new_schema and old_table != new_table:
            temporary_table = Identifiers.get_temporary_name("hare_partition")
            steps.append(SchemaStatement(self.editor.get_table_rename_sql(old_table, temporary_table, old_schema)))
            steps.extend(self.editor.schemas.get_table_schema_move_plan(temporary_table, old_schema, new_schema).steps)
            steps.append(SchemaStatement(self.editor.get_table_rename_sql(temporary_table, new_table, new_schema)))
        elif old_table != new_table:
            steps.append(SchemaStatement(self.editor.get_table_rename_sql(old_table, new_table, old_schema)))
        else:
            steps.extend(self.editor.schemas.get_table_schema_move_plan(old_table, old_schema, new_schema).steps)
        return SchemaChangePlan(tuple(steps))

    def get_bound_value_renderer(self, binding: ModelBinding, partitioning: Partitioning) -> BoundValueRenderer:
        """How the values of a partition's bound are written for a model's key columns.

        Args:
            binding: The partitioned model.
            partitioning: Its partitioning.

        Returns:
            Renders the value of the key column at a position as an SQL literal.
        """
        model = binding._model
        key_fields = partitioning.get_key_fields(binding)

        def render_value(position: int, value: Any) -> str:
            if value is None:
                return "NULL"
            db_value = self.editor.client.dialect.types.get_db_value(key_fields[position], value, model)
            return self.editor.client.dialect.literals.get_literal_sql(db_value)

        return render_value

    def get_partition_create_sql(
        self,
        binding: ModelBinding,
        options: PostgresqlTableOptions,
        location: tuple[str | None, str],
        bound_sql: str,
        safe: bool,
    ) -> str:
        """The statement creating one partition - with the storage parameters and tablespace of the
        table's options, which a partitioned table itself can't hold.

        Args:
            binding: The partitioned model.
            options: Its table options.
            location: The partition's physical schema and table.
            bound_sql: Its bound (``FOR VALUES ...``/``DEFAULT``).
            safe: Whether it is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        schema, partition_table = location
        return POSTGRESQL_PARTITION_CREATE_TEMPLATE.format(
            exists=self.editor.table_creation.get_exists_sql(safe),
            partition=self.editor.qualify_table_name(partition_table, schema),
            table=self.editor.qualify_table_name(binding.db_table, binding.schema),
            bound=bound_sql,
            storage=options.get_storage_sql(self.editor),
        )

    def get_stored_table_names(
        self, binding: ModelBinding, options: PostgresqlTableOptions, skipped_partition_names: Collection[str] = ()
    ) -> list[str]:
        """The tables holding a model's rows - its table, or each partition of a partitioned one.

        Args:
            binding: The model.
            options: Its table options.
            skipped_partition_names: Partitions to leave out.

        Returns:
            The qualified table names.
        """
        if options.partitioning is None:
            return [self.editor.qualify_table_name(binding.db_table, binding.schema)]
        return [
            self.editor.qualify_table_name(table, schema)
            for name, (schema, table) in options.partitioning.get_partition_locations(
                binding.db_table, binding.schema
            ).items()
            if name not in skipped_partition_names
        ]

    def get_tablespace_sql(self, qualified_table: str, tablespace: str | None) -> str:
        """Compile a named tablespace move or the server's runtime default lookup."""
        if tablespace:
            return f"ALTER TABLE {qualified_table} SET TABLESPACE {self.editor.quote(tablespace)}"
        table_literal = self.editor.client.dialect.literals.get_string_literal_sql(qualified_table)
        return self.editor.get_anonymous_block_sql(POSTGRESQL_SET_DEFAULT_TABLESPACE_BODY.format(table=table_literal))
