from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
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
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.postgresql.declarations import PostgresqlFeatures
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTablePartitions(TablePartitions):
    """TablePartitions as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_alter_table_options_plan(self, old_model: type[Model], new_model: type[Model]) -> SchemaChangePlan:
        """Compile partition and storage changes from the complete before/after models.

        Changing the partitioning strategy uses the same rebuild plan as execution
        and preview. In-place changes are fully rendered before any partition is
        detached, including bounds and options of partitions added later.
        """
        model = new_model
        dialect = self.editor.client.dialect
        old = cast("PostgresqlTableOptions", old_model._meta.get_table_options(dialect) or PostgresqlTableOptions())
        new = cast("PostgresqlTableOptions", new_model._meta.get_table_options(dialect) or PostgresqlTableOptions())
        new.raise_if_unsupported(model, self.editor.client.features)
        if old.with_partitions({}).partitioning != new.with_partitions({}).partitioning:
            return self.editor.table_rebuild.get_rebuild_plan(old_model, new_model)
        old_partitions, new_partitions = old.get_partitions(), new.get_partitions()
        steps: list[SchemaStep] = []
        for name, partition in old_partitions.items():
            if new_partitions.get(name) != partition:
                steps.extend(self.get_remove_partition_plan(old_model, partition).steps)
        qualified_table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        added_partition_names = {
            name for name, partition in new_partitions.items() if old_partitions.get(name) != partition
        }
        stored_tables = self.get_stored_table_names(model, new, skipped_partition_names=added_partition_names)
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
        for name, partition in new_partitions.items():
            if old_partitions.get(name) != partition:
                steps.extend(self.get_add_partition_plan(model, partition).steps)
        return SchemaChangePlan(tuple(steps))

    def get_partition_create_sqls(self, model: type[Model], safe: bool) -> list[str]:
        options = self.editor.get_partitioned_options(model)
        if options is None:
            return []
        features = cast("PostgresqlFeatures", self.editor.client.features)
        if not features.supports_partitioned_exclusion_constraints and any(
            isinstance(constraint, ExclusionConstraint) for constraint in model._meta.constraints
        ):
            raise UnSupportedError(
                f"{model.__name__}: a partitioned table takes an exclusion constraint from PostgreSQL 17 on"
            )
        partitioning = cast("Partitioning", options.partitioning)
        bound_sqls = partitioning.get_partition_bound_sqls(self.get_bound_value_renderer(model, partitioning))
        return [
            self.get_partition_create_sql(model, options, partition_name, bound_sql, safe)
            for partition_name, bound_sql in bound_sqls.items()
        ]

    def get_partition_table_names(self, model: type[Model]) -> list[str]:
        options = self.editor.get_partitioned_options(model)
        if options is None:
            return []
        return [
            Partitioning.get_partition_table_name(model._meta.db_table, partition_name)
            for partition_name in cast("Partitioning", options.partitioning).get_partition_names()
        ]

    async def add_partition(self, model: type[Model], partition: Partition) -> None:
        await self.get_add_partition_plan(model, partition).execute(self.editor)

    def get_add_partition_plan(self, model: type[Model], partition: Partition) -> SchemaChangePlan:
        """Creates a partition of a model's partitioned table.

        Args:
            model: The model rendered with the partition.
            partition: The partition.

        Raises:
            ConfigurationError: The model's table isn't partitioned.
        """
        options = self.editor.get_partitioned_options(model)
        if options is None:
            raise ConfigurationError(f"{model.__name__}: a partition can only be added to a partitioned table")
        partitioning = cast("Partitioning", options.partitioning)
        bound_sql = partition.get_bound_sql(self.get_bound_value_renderer(model, partitioning))
        return SchemaChangePlan(
            (SchemaStatement(self.get_partition_create_sql(model, options, partition.name, bound_sql, safe=False)),)
        )

    async def remove_partition(self, model: type[Model], partition: Partition) -> None:
        await self.get_remove_partition_plan(model, partition).execute(self.editor)

    def get_remove_partition_plan(self, model: type[Model], partition: Partition) -> SchemaChangePlan:
        """Detaches a partition from a model's table and drops it - with its rows.

        Args:
            model: The model.
            partition: The partition.
        """
        schema = model._meta.schema
        qualified_partition = self.editor.qualify_table_name(
            Partitioning.get_partition_table_name(model._meta.db_table, partition.name), schema
        )
        qualified_table = self.editor.qualify_table_name(model._meta.db_table, schema)
        return SchemaChangePlan(
            (
                SchemaStatement(f"ALTER TABLE {qualified_table} DETACH PARTITION {qualified_partition}"),
                SchemaStatement(f"DROP TABLE {qualified_partition}"),
            )
        )

    def get_bound_value_renderer(self, model: type[Model], partitioning: Partitioning) -> BoundValueRenderer:
        """How the values of a partition's bound are written for a model's key columns.

        Args:
            model: The partitioned model.
            partitioning: Its partitioning.

        Returns:
            Renders the value of the key column at a position as an SQL literal.
        """
        key_fields = partitioning.get_key_fields(model)

        def render_value(position: int, value: Any) -> str:
            if value is None:
                return "NULL"
            db_value = self.editor.client.dialect.types.get_db_value(key_fields[position], value, model)
            return self.editor.client.dialect.literals.get_literal_sql(db_value)

        return render_value

    def get_partition_create_sql(
        self, model: type[Model], options: PostgresqlTableOptions, partition_name: str, bound_sql: str, safe: bool
    ) -> str:
        """The statement creating one partition - with the storage parameters and tablespace of the
        table's options, which a partitioned table itself can't hold.

        Args:
            model: The partitioned model.
            options: Its table options.
            partition_name: The partition's name.
            bound_sql: Its bound (``FOR VALUES ...``/``DEFAULT``).
            safe: Whether it is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        schema = model._meta.schema
        partition_table = Partitioning.get_partition_table_name(model._meta.db_table, partition_name)
        return POSTGRESQL_PARTITION_CREATE_TEMPLATE.format(
            exists=self.editor.table_creation.get_exists_sql(safe),
            partition=self.editor.qualify_table_name(partition_table, schema),
            table=self.editor.qualify_table_name(model._meta.db_table, schema),
            bound=bound_sql,
            storage=options.get_storage_sql(self.editor),
        )

    def get_stored_table_names(
        self, model: type[Model], options: PostgresqlTableOptions, skipped_partition_names: Collection[str] = ()
    ) -> list[str]:
        """The tables holding a model's rows - its table, or each partition of a partitioned one.

        Args:
            model: The model.
            options: Its table options.
            skipped_partition_names: Partitions to leave out.

        Returns:
            The qualified table names.
        """
        schema = model._meta.schema
        if options.partitioning is None:
            return [self.editor.qualify_table_name(model._meta.db_table, schema)]
        return [
            self.editor.qualify_table_name(Partitioning.get_partition_table_name(model._meta.db_table, name), schema)
            for name in options.partitioning.get_partition_names()
            if name not in skipped_partition_names
        ]

    def get_tablespace_sql(self, qualified_table: str, tablespace: str | None) -> str:
        """Compile a named tablespace move or the server's runtime default lookup."""
        if tablespace:
            return f"ALTER TABLE {qualified_table} SET TABLESPACE {self.editor.quote(tablespace)}"
        table_literal = self.editor.client.dialect.literals.get_string_literal_sql(qualified_table)
        return self.editor.get_anonymous_block_sql(POSTGRESQL_SET_DEFAULT_TABLESPACE_BODY.format(table=table_literal))
