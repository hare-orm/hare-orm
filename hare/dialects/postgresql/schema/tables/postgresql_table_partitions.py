from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.table_options import TableOptions
from hare.dialects.base.schema.tables.table_partitions import TablePartitions
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.dialects.postgresql.partitioning.partitions.partition import Partition
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_INCOMING_FOREIGN_KEYS_SQL,
    POSTGRESQL_PARTITION_CREATE_TEMPLATE,
    POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE,
    POSTGRESQL_SET_DEFAULT_TABLESPACE_SQL,
    POSTGRESQL_TABLE_COPY_SUFFIX,
)
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.models import Model
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTablePartitions(TablePartitions):
    """TablePartitions as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    async def alter_table_options(
        self, model: type[Model], old_options: TableOptions | None, new_options: TableOptions | None
    ) -> None:
        """Changes a table's storage in place: ``SET TABLESPACE``, ``SET LOGGED``/``SET UNLOGGED``,
        ``SET (...)``/``RESET (...)`` of the storage parameters that changed - on each partition of
        a partitioned table, which holds none itself - and the partitions added or removed. A change
        of how the table is partitioned (to or from a partitioned table, another strategy or key,
        another count of hash partitions) can't be made in place: the table is created anew and
        its rows are copied.

        Args:
            model: The model rendered with its new options.
            old_options: The previous ``PostgresqlTableOptions``, None for none.
            new_options: The new ``PostgresqlTableOptions``, None for none.
        """
        old = cast("PostgresqlTableOptions", old_options or PostgresqlTableOptions())
        new = cast("PostgresqlTableOptions", new_options or PostgresqlTableOptions())
        if old.with_partitions({}).partitioning != new.with_partitions({}).partitioning:
            await self.recreate_table(model)
            return
        old_partitions, new_partitions = old.get_partitions(), new.get_partitions()
        for name, partition in old_partitions.items():
            if new_partitions.get(name) != partition:
                await self.remove_partition(model, partition)
        qualified_table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        # A partition added below is created with the new options already.
        added_partition_names = {
            name for name, partition in new_partitions.items() if old_partitions.get(name) != partition
        }
        stored_tables = self.get_stored_table_names(model, new, skipped_partition_names=added_partition_names)
        if old.tablespace != new.tablespace:
            # A partitioned table's own tablespace is the one its new partitions are created in.
            for table in dict.fromkeys([qualified_table, *stored_tables]):
                await self.set_tablespace(table, new.tablespace)
        if old.unlogged != new.unlogged:
            await self.editor.run_sql(f"ALTER TABLE {qualified_table} SET {'UNLOGGED' if new.unlogged else 'LOGGED'}")
        removed_parameters = [name for name in old.storage_parameters if name not in new.storage_parameters]
        changed_parameters = {
            name: value
            for name, value in new.storage_parameters.items()
            if name not in old.storage_parameters or old.storage_parameters[name] != value
        }
        for stored_table in stored_tables:
            if removed_parameters:
                await self.editor.run_sql(f"ALTER TABLE {stored_table} RESET ({', '.join(removed_parameters)})")
            if changed_parameters:
                await self.editor.run_sql(
                    f"ALTER TABLE {stored_table} SET ({new.get_storage_parameters_sql(changed_parameters)})"
                )
        for name, partition in new_partitions.items():
            if old_partitions.get(name) != partition:
                await self.add_partition(model, partition)

    def get_partition_create_sqls(self, model: type[Model], safe: bool) -> list[str]:
        options = self.editor.get_partitioned_options(model)
        if options is None:
            return []
        if not self.editor.client.features.supports_partitioned_exclusion_constraints and any(
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

    async def add_partition(self, model: type[Model], partition: Partition) -> None:
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
        await self.editor.run_sql(self.get_partition_create_sql(model, options, partition.name, bound_sql, safe=False))

    async def remove_partition(self, model: type[Model], partition: Partition) -> None:
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
        await self.editor.run_sql(f"ALTER TABLE {qualified_table} DETACH PARTITION {qualified_partition}")
        await self.editor.run_sql(f"DROP TABLE {qualified_partition}")

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
            storage=options.get_storage_sql(self.editor.quote),
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

    async def set_tablespace(self, qualified_table: str, tablespace: str | None) -> None:
        """Moves a table to a tablespace.

        Args:
            qualified_table: The table.
            tablespace: The tablespace, None for the database's default one.
        """
        if tablespace:
            await self.editor.run_sql(f"ALTER TABLE {qualified_table} SET TABLESPACE {self.editor.quote(tablespace)}")
        else:
            table_literal = "'" + qualified_table.replace("'", "''") + "'"
            await self.editor.run_sql(POSTGRESQL_SET_DEFAULT_TABLESPACE_SQL.format(table=table_literal))

    async def recreate_table(self, model: type[Model]) -> None:
        """Creates a model's table anew with its rows - for a change PostgreSQL can't make in
        place, such as how the table is partitioned. The rows are kept in a copy of the table
        while the table is dropped and created from the model with its partitions, indexes,
        constraints, comments and triggers; then the rows are put back, the sequences of generated
        keys moved past them and the foreign keys of other tables referencing the table set again.

        Args:
            model: The model rendered from the state the table is created to.
        """
        meta = model._meta
        qualified_table = self.editor.qualify_table_name(meta.db_table, meta.schema)
        qualified_copy = self.editor.qualify_table_name(
            Identifiers.get_within_limit(f"{meta.db_table}{POSTGRESQL_TABLE_COPY_SUFFIX}"), meta.schema
        )
        incoming_foreign_keys: list[dict[str, Any]] = []
        if not self.editor.collect_sql:
            incoming_foreign_keys = await self.editor.client.execute_dicts(
                POSTGRESQL_INCOMING_FOREIGN_KEYS_SQL, [meta.db_table, meta.schema]
            )
        for foreign_key in incoming_foreign_keys:
            await self.editor.run_sql(
                f"ALTER TABLE {foreign_key['table_sql']} DROP CONSTRAINT {self.editor.quote(foreign_key['name'])}"
            )
        await self.editor.run_sql(f"CREATE TABLE {qualified_copy} AS SELECT * FROM {qualified_table}")  # nosec B608
        await self.editor.run_sql(f"DROP TABLE {qualified_table}")
        model_sql_data = self.editor.table_creation.get_model_sql_data(model)
        await self.editor.run_sql(model_sql_data.table_sql)
        for trigger in meta.triggers:
            for statement in self.editor.trigger_statements.get_trigger_create_sqls(model, trigger, safe=True):
                await self.editor.run_sql(statement)
        for constraint_sql in model_sql_data.constraint_sqls:
            await self.editor.run_sql(constraint_sql)
        copied_columns = ", ".join(
            self.editor.quote(column) for column in self.editor.table_rebuild.build_remake_column_mapping(model)
        )
        await self.editor.run_sql(
            f"INSERT INTO {qualified_table} ({copied_columns}) SELECT {copied_columns} FROM {qualified_copy}"  # nosec B608
        )
        await self.editor.run_sql(f"DROP TABLE {qualified_copy}")
        table_literal = "'" + qualified_table.replace("'", "''") + "'"
        for field in meta.fields_map.values():
            if field.pk and field.generated and field.model_field_name in meta.fields_db_projection:
                column = meta.fields_db_projection[field.model_field_name]
                await self.editor.run_sql(
                    POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE.format(
                        table_literal=table_literal,
                        column_literal="'" + column.replace("'", "''") + "'",
                        column=self.editor.quote(column),
                        table=qualified_table,
                    )
                )
        for foreign_key in incoming_foreign_keys:
            await self.editor.run_sql(
                f"ALTER TABLE {foreign_key['table_sql']} ADD CONSTRAINT {self.editor.quote(foreign_key['name'])} "
                f"{foreign_key['definition']}"
            )
