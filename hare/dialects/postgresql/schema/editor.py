from collections.abc import Collection, Iterable, Sequence
from typing import Any, cast

from hare.ddl.conditions import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.table_options import TableOptions
from hare.ddl.triggers import Trigger
from hare.dialects.base.schema.editor import BaseSchemaEditor
from hare.dialects.identifiers import Identifiers
from hare.dialects.postgresql.constants import (
    POSTGRESQL_COLUMN_COMMENT_TEMPLATE,
    POSTGRESQL_CONCURRENT_INDEX_CREATE_PATTERN,
    POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE,
    POSTGRESQL_EXCLUSION_CONSTRAINT_CREATE_TEMPLATE,
    POSTGRESQL_GENERATED_PK_TEMPLATE,
    POSTGRESQL_INCOMING_FOREIGN_KEYS_SQL,
    POSTGRESQL_MOVE_TABLE_TO_CURRENT_SCHEMA_TEMPLATE,
    POSTGRESQL_PARTITION_CREATE_TEMPLATE,
    POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE,
    POSTGRESQL_SET_DEFAULT_TABLESPACE_SQL,
    POSTGRESQL_TABLE_COMMENT_TEMPLATE,
    POSTGRESQL_TABLE_CONSTRAINT_NAMES_SQL,
    POSTGRESQL_TABLE_COPY_SUFFIX,
    POSTGRESQL_TABLE_SEQUENCE_NAMES_SQL,
)
from hare.dialects.postgresql.partitioning.partition import Partition
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.dialects.postgresql.table_options import PostgresqlTableOptions
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.base.field import Field
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model


class PostgresqlSchemaEditor(BaseSchemaEditor):
    INDEX_CREATE_TEMPLATE = "CREATE INDEX {exists}{index_name} ON {table_name} {index_type}({fields}){extra};"
    UNIQUE_INDEX_CREATE_TEMPLATE = INDEX_CREATE_TEMPLATE.replace("INDEX", "UNIQUE INDEX")
    EXCLUSION_CONSTRAINT_CREATE_TEMPLATE = POSTGRESQL_EXCLUSION_CONSTRAINT_CREATE_TEMPLATE
    TABLE_COMMENT_TEMPLATE = POSTGRESQL_TABLE_COMMENT_TEMPLATE
    COLUMN_COMMENT_TEMPLATE = POSTGRESQL_COLUMN_COMMENT_TEMPLATE
    GENERATED_PK_TEMPLATE = POSTGRESQL_GENERATED_PK_TEMPLATE
    # USING casts explicitly - without it only a type with an implicit cast is accepted.
    ALTER_FIELD_TYPE_TEMPLATE = "ALTER COLUMN {column} TYPE {sql_type} USING {column}::{sql_type}"
    RENAME_INDEX_TEMPLATE: str | None = "ALTER INDEX {old_name} RENAME TO {new_name}"
    RENAME_INDEX_IF_EXISTS_TEMPLATE: str | None = "ALTER INDEX IF EXISTS {old_name} RENAME TO {new_name}"
    TRIGGER_FUNCTION_CREATE_TEMPLATE = (
        "CREATE FUNCTION {function_name}() RETURNS TRIGGER AS $trigger$\nBEGIN\n{body}\nEND;\n"
        "$trigger$ LANGUAGE {language};"
    )
    TRIGGER_FUNCTION_CREATE_OR_REPLACE_TEMPLATE = TRIGGER_FUNCTION_CREATE_TEMPLATE.replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
    )
    TRIGGER_FUNCTION_DROP_TEMPLATE = "DROP FUNCTION {function_name}()"
    # {trigger_name}/{old_name}/{new_name} are passed already quoted.
    TRIGGER_CREATE_TEMPLATE = (
        "CREATE TRIGGER {trigger_name} {timing} {on} ON {table}\n"
        "    FOR EACH {for_each}{when_clause}\n"
        "    EXECUTE FUNCTION {function_name}();"
    )
    #: For Trigger.deferrable=True - a constraint trigger, always AFTER and FOR EACH ROW.
    TRIGGER_CONSTRAINT_CREATE_TEMPLATE = (
        "CREATE CONSTRAINT TRIGGER {trigger_name} {timing} {on} ON {table}\n"
        "    DEFERRABLE INITIALLY {initially}\n"
        "    FOR EACH {for_each}{when_clause}\n"
        "    EXECUTE FUNCTION {function_name}();"
    )
    TRIGGER_DROP_TEMPLATE = "DROP TRIGGER {trigger_name} ON {table}"
    TRIGGER_DROP_IF_EXISTS_TEMPLATE = "DROP TRIGGER IF EXISTS {trigger_name} ON {table};"
    RENAME_TRIGGER_TEMPLATE: str | None = "ALTER TRIGGER {old_name} ON {table} RENAME TO {new_name}"
    RENAME_TRIGGER_FUNCTION_TEMPLATE: str | None = "ALTER FUNCTION {function_name}() RENAME TO {new_function_name}"

    def __init__(self, connection, atomic: bool = True, collect_sql: bool = False) -> None:
        super().__init__(connection, atomic, collect_sql=collect_sql)
        self.comments_array: list[str] = []

    def _get_table_comment_sql(self, table: str, comment: str) -> str:
        sql = self.TABLE_COMMENT_TEMPLATE.format(
            table=table, comment=self.client.dialect.get_string_literal_sql(comment)
        )
        if sql not in self.comments_array:
            self.comments_array.append(sql)
        return ""

    def _get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        sql = self.COLUMN_COMMENT_TEMPLATE.format(
            table=table, column=self.quote(column), comment=self.client.dialect.get_string_literal_sql(comment)
        )
        if sql not in self.comments_array:
            self.comments_array.append(sql)
        return ""

    def _post_table_hook(self) -> str:
        sql = "\n".join(self.comments_array)
        self.comments_array = []
        if sql:
            return "\n" + sql
        return ""

    async def _replace_table(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> None:
        """Drops a table and renames its rebuilt copy into its place, then renames what PostgreSQL
        named after the copy's temporary name - its implicit constraints (``<table>_pkey``,
        ``<table>_<column>_key``, ``<table>_<column>_fkey``) and serial sequences - and writes the
        column comments the rebuilt table's definition collected.

        Args:
            table_name: The table being replaced.
            rebuilt_table_name: The already filled copy taking its place.
            schema: The tables' schema.
            fields: The fields of the rebuilt table's columns.
        """
        await super()._replace_table(table_name, rebuilt_table_name, schema, fields)
        qualified_table = self._qualify_table_name(table_name, schema)
        if not self.collect_sql:
            temporary_prefix = f"{rebuilt_table_name}_"
            constraint_rows = await self.client.execute_dicts(
                POSTGRESQL_TABLE_CONSTRAINT_NAMES_SQL, [table_name, schema]
            )
            for row in constraint_rows:
                if row["name"].startswith(temporary_prefix):
                    new_name = f"{table_name}_{row['name'].removeprefix(temporary_prefix)}"
                    await self._run_sql(
                        f"ALTER TABLE {qualified_table} RENAME CONSTRAINT {self.quote(row['name'])} "
                        f"TO {self.quote(new_name)}"
                    )
            sequence_rows = await self.client.execute_dicts(POSTGRESQL_TABLE_SEQUENCE_NAMES_SQL, [table_name, schema])
            for row in sequence_rows:
                if row["name"].startswith(temporary_prefix):
                    new_name = f"{table_name}_{row['name'].removeprefix(temporary_prefix)}"
                    await self._run_sql(
                        f"ALTER SEQUENCE {self._qualify_table_name(row['name'], schema)} "
                        f"RENAME TO {self.quote(new_name)}"
                    )
        if comments_sql := self._post_table_hook():
            await self._run_sql(comments_sql.strip())

    def _get_partitioned_options(self, model: type[Model]) -> PostgresqlTableOptions | None:
        """The table options of a model whose table is partitioned.

        Args:
            model: The model.

        Returns:
            The options, None for a plain table.
        """
        options = cast("PostgresqlTableOptions | None", model._meta.get_table_options(self.client.dialect))
        return options if options is not None and options.partitioning is not None else None

    def _get_bound_value_renderer(self, model: type[Model], partitioning: Partitioning) -> BoundValueRenderer:
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
            db_value = self.client.dialect.types.get_db_value(key_fields[position], value, model)
            return self.client.dialect.get_literal_sql(db_value)

        return render_value

    def _get_partition_create_sql(
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
            exists=self._get_exists_sql(safe),
            partition=self._qualify_table_name(partition_table, schema),
            table=self._qualify_table_name(model._meta.db_table, schema),
            bound=bound_sql,
            storage=options.get_storage_sql(self.quote),
        )

    def _get_partition_create_sqls(self, model: type[Model], safe: bool) -> list[str]:
        options = self._get_partitioned_options(model)
        if options is None:
            return []
        if not self.client.features.supports_partitioned_exclusion_constraints and any(
            isinstance(constraint, ExclusionConstraint) for constraint in model._meta.constraints
        ):
            raise UnSupportedError(
                f"{model.__name__}: a partitioned table takes an exclusion constraint from PostgreSQL 17 on"
            )
        partitioning = cast("Partitioning", options.partitioning)
        bound_sqls = partitioning.get_partition_bound_sqls(self._get_bound_value_renderer(model, partitioning))
        return [
            self._get_partition_create_sql(model, options, partition_name, bound_sql, safe)
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
        options = self._get_partitioned_options(model)
        if options is None:
            raise ConfigurationError(f"{model.__name__}: a partition can only be added to a partitioned table")
        partitioning = cast("Partitioning", options.partitioning)
        bound_sql = partition.get_bound_sql(self._get_bound_value_renderer(model, partitioning))
        await self._run_sql(self._get_partition_create_sql(model, options, partition.name, bound_sql, safe=False))

    async def remove_partition(self, model: type[Model], partition: Partition) -> None:
        """Detaches a partition from a model's table and drops it - with its rows.

        Args:
            model: The model.
            partition: The partition.
        """
        schema = model._meta.schema
        qualified_partition = self._qualify_table_name(
            Partitioning.get_partition_table_name(model._meta.db_table, partition.name), schema
        )
        qualified_table = self._qualify_table_name(model._meta.db_table, schema)
        await self._run_sql(f"ALTER TABLE {qualified_table} DETACH PARTITION {qualified_partition}")
        await self._run_sql(f"DROP TABLE {qualified_partition}")

    def _get_stored_table_names(
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
            return [self._qualify_table_name(model._meta.db_table, schema)]
        return [
            self._qualify_table_name(Partitioning.get_partition_table_name(model._meta.db_table, name), schema)
            for name in options.partitioning.get_partition_names()
            if name not in skipped_partition_names
        ]

    async def _set_tablespace(self, qualified_table: str, tablespace: str | None) -> None:
        """Moves a table to a tablespace.

        Args:
            qualified_table: The table.
            tablespace: The tablespace, None for the database's default one.
        """
        if tablespace:
            await self._run_sql(f"ALTER TABLE {qualified_table} SET TABLESPACE {self.quote(tablespace)}")
        else:
            table_literal = "'" + qualified_table.replace("'", "''") + "'"
            await self._run_sql(POSTGRESQL_SET_DEFAULT_TABLESPACE_SQL.format(table=table_literal))

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
            await self._recreate_table(model)
            return
        old_partitions, new_partitions = old.get_partitions(), new.get_partitions()
        for name, partition in old_partitions.items():
            if new_partitions.get(name) != partition:
                await self.remove_partition(model, partition)
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        # A partition added below is created with the new options already.
        added_partition_names = {
            name for name, partition in new_partitions.items() if old_partitions.get(name) != partition
        }
        stored_tables = self._get_stored_table_names(model, new, skipped_partition_names=added_partition_names)
        if old.tablespace != new.tablespace:
            # A partitioned table's own tablespace is the one its new partitions are created in.
            for table in dict.fromkeys([qualified_table, *stored_tables]):
                await self._set_tablespace(table, new.tablespace)
        if old.unlogged != new.unlogged:
            await self._run_sql(f"ALTER TABLE {qualified_table} SET {'UNLOGGED' if new.unlogged else 'LOGGED'}")
        removed_parameters = [name for name in old.storage_parameters if name not in new.storage_parameters]
        changed_parameters = {
            name: value
            for name, value in new.storage_parameters.items()
            if name not in old.storage_parameters or old.storage_parameters[name] != value
        }
        for stored_table in stored_tables:
            if removed_parameters:
                await self._run_sql(f"ALTER TABLE {stored_table} RESET ({', '.join(removed_parameters)})")
            if changed_parameters:
                await self._run_sql(
                    f"ALTER TABLE {stored_table} SET ({new.get_storage_parameters_sql(changed_parameters)})"
                )
        for name, partition in new_partitions.items():
            if old_partitions.get(name) != partition:
                await self.add_partition(model, partition)

    async def _recreate_table(self, model: type[Model]) -> None:
        """Creates a model's table anew with its rows - for a change PostgreSQL can't make in
        place, such as how the table is partitioned. The rows are kept in a copy of the table
        while the table is dropped and created from the model with its partitions, indexes,
        constraints, comments and triggers; then the rows are put back, the sequences of generated
        keys moved past them and the foreign keys of other tables referencing the table set again.

        Args:
            model: The model rendered from the state the table is created to.
        """
        meta = model._meta
        qualified_table = self._qualify_table_name(meta.db_table, meta.schema)
        qualified_copy = self._qualify_table_name(
            Identifiers.get_within_limit(f"{meta.db_table}{POSTGRESQL_TABLE_COPY_SUFFIX}"), meta.schema
        )
        incoming_foreign_keys: list[dict[str, Any]] = []
        if not self.collect_sql:
            incoming_foreign_keys = await self.client.execute_dicts(
                POSTGRESQL_INCOMING_FOREIGN_KEYS_SQL, [meta.db_table, meta.schema]
            )
        for foreign_key in incoming_foreign_keys:
            await self._run_sql(
                f"ALTER TABLE {foreign_key['table_sql']} DROP CONSTRAINT {self.quote(foreign_key['name'])}"
            )
        await self._run_sql(f"CREATE TABLE {qualified_copy} AS SELECT * FROM {qualified_table}")  # nosec B608
        await self._run_sql(f"DROP TABLE {qualified_table}")
        model_sql_data = self._get_model_sql_data(model)
        await self._run_sql(model_sql_data.table_sql)
        for trigger in meta.triggers:
            for statement in self.get_trigger_create_sqls(model, trigger, safe=True):
                await self._run_sql(statement)
        for constraint_sql in model_sql_data.constraint_sqls:
            await self._run_sql(constraint_sql)
        copied_columns = ", ".join(self.quote(column) for column in self._build_remake_column_mapping(model))
        await self._run_sql(
            f"INSERT INTO {qualified_table} ({copied_columns}) SELECT {copied_columns} FROM {qualified_copy}"  # nosec B608
        )
        await self._run_sql(f"DROP TABLE {qualified_copy}")
        table_literal = "'" + qualified_table.replace("'", "''") + "'"
        for field in meta.fields_map.values():
            if field.pk and field.generated and field.model_field_name in meta.fields_db_projection:
                column = meta.fields_db_projection[field.model_field_name]
                await self._run_sql(
                    POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE.format(
                        table_literal=table_literal,
                        column_literal="'" + column.replace("'", "''") + "'",
                        column=self.quote(column),
                        table=qualified_table,
                    )
                )
        for foreign_key in incoming_foreign_keys:
            await self._run_sql(
                f"ALTER TABLE {foreign_key['table_sql']} ADD CONSTRAINT {self.quote(foreign_key['name'])} "
                f"{foreign_key['definition']}"
            )

    async def rename_table(self, model: type[Model], old_name: str, new_name: str) -> None:
        """Renames a table and, for a partitioned one, its partitions - named after it.

        Args:
            model: The model.
            old_name: The table's name.
            new_name: Its new name.
        """
        await super().rename_table(model, old_name, new_name)
        options = self._get_partitioned_options(model)
        if options is None or old_name == new_name:
            return
        for partition_name in cast("Partitioning", options.partitioning).get_partition_names():
            old_partition = Partitioning.get_partition_table_name(old_name, partition_name)
            new_partition = Partitioning.get_partition_table_name(new_name, partition_name)
            await self._run_sql(
                self.RENAME_TABLE_TEMPLATE.format(
                    old_table=self._qualify_table_name(old_partition, model._meta.schema),
                    new_table=self.quote(new_partition),
                )
            )

    async def add_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Creates an index. PostgreSQL can't build one concurrently on a partitioned table as a
        whole: the index is created on the table alone, built concurrently on each partition and
        attached, which makes the table's index valid once every partition has its own.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Build it without blocking writes.
        """
        if not concurrently:
            await super().add_index(model, index)
            return
        options = self._get_partitioned_options(model)
        if options is None:
            await self._run_sql(self._get_concurrent_index_create_sql(self._get_index_create_sql(model, index)))
            return
        schema = model._meta.schema
        index_name = self._index_name_for_model(model, index)
        qualified_table = self._qualify_table_name(model._meta.db_table, schema)
        await self._run_sql(self._get_index_create_sql(model, index, indexed_table_sql=f"ONLY {qualified_table}"))
        for partition_name in cast("Partitioning", options.partitioning).get_partition_names():
            partition_table = Partitioning.get_partition_table_name(model._meta.db_table, partition_name)
            partition_index_name = Identifiers.get_within_limit(f"{partition_table}_{index_name}")
            partition_index_sql = self._get_index_create_sql(
                model, index, partition_index_name, self._qualify_table_name(partition_table, schema)
            )
            await self._run_sql(self._get_concurrent_index_create_sql(partition_index_sql))
            await self._run_sql(
                f"ALTER INDEX {self._qualify_table_name(index_name, schema)} "
                f"ATTACH PARTITION {self._qualify_table_name(partition_index_name, schema)}"
            )

    async def remove_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Drops an index - the plain way on a partitioned table, whose index PostgreSQL can't
        drop concurrently.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Drop it without blocking reads and writes.
        """
        drop_sql = self._get_index_drop_sql(model, index)
        if concurrently and self._get_partitioned_options(model) is None:
            drop_sql = drop_sql.replace("DROP INDEX ", "DROP INDEX CONCURRENTLY ", 1)
        await self._run_sql(drop_sql)

    @staticmethod
    def _get_concurrent_index_create_sql(index_sql: str) -> str:
        """Returns a ``CREATE INDEX`` statement building the index without blocking writes.

        Args:
            index_sql: The plain statement.

        Returns:
            The statement.
        """
        return POSTGRESQL_CONCURRENT_INDEX_CREATE_PATTERN.sub(r"\1CONCURRENTLY ", index_sql, count=1)

    def _exclusion_constraint_sql(self, model: type[Model], constraint: ExclusionConstraint) -> str:
        expression_sqls = []
        for expression, operator in constraint.expressions:
            # A field name is its quoted column; a RawSQLTerm is spliced in as written.
            expression_sql = (
                expression.get_sql()
                if isinstance(expression, RawSQLTerm)
                else self.quote(self._get_fields_to_columns(model, [expression])[0])
            )
            expression_sqls.append(f"{expression_sql} WITH {operator}")
        clauses_sql = ""
        if constraint.include:
            clauses_sql += self.client.dialect.get_index_include_sql(
                [self.quote(column) for column in model._meta.get_column_names(constraint.include)]
            )
        if constraint.condition:
            clauses_sql += f" WHERE ({ConstraintCondition.get_sql(constraint.condition, model, self.client)})"
        if constraint.deferrable:
            clauses_sql += " DEFERRABLE INITIALLY " + ("DEFERRED" if constraint.initially_deferred else "IMMEDIATE")
        return self.EXCLUSION_CONSTRAINT_CREATE_TEMPLATE.format(
            name=self.quote(constraint.name),
            using=constraint.using,
            expressions=", ".join(expression_sqls),
            where=clauses_sql,
        )

    async def add_check_constraint_not_valid(self, model: type[Model], constraint: CheckConstraint) -> None:
        constraint_sql = self.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
            name=self.quote(constraint.name),
            check=ConstraintCondition.get_sql(constraint.check, model, self.client),
        )
        await self._run_sql(
            self.ADD_CONSTRAINT_TEMPLATE.format(
                table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=f"{constraint_sql} NOT VALID",
            )
        )

    async def validate_constraint(self, model: type[Model], name: str) -> None:
        table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        await self._run_sql(f"ALTER TABLE {table} VALIDATE CONSTRAINT {self.quote(name)}")

    def _get_extension_create_sql(self, extension: str) -> str:
        return f"CREATE EXTENSION IF NOT EXISTS {self.quote(extension)};"

    async def create_extension(self, extension_name: str) -> None:
        await self._run_sql(self._get_extension_create_sql(extension_name))

    async def drop_extension(self, extension_name: str) -> None:
        await self._run_sql(f"DROP EXTENSION IF EXISTS {self.quote(extension_name)};")

    async def create_collation(self, name: str, locale: str, provider: str, deterministic: bool) -> None:
        locale_literal = "'" + locale.replace("'", "''") + "'"
        options = [f"locale = {locale_literal}", f"provider = {provider}"]
        if not deterministic:
            options.append("deterministic = false")
        await self._run_sql(f"CREATE COLLATION {self.quote(name)} ({', '.join(options)});")

    async def drop_collation(self, name: str) -> None:
        await self._run_sql(f"DROP COLLATION {self.quote(name)};")

    async def move_table_to_schema(self, table_name: str, old_schema: str | None, new_schema: str | None) -> None:
        """Moves a table to another schema with ``ALTER TABLE ... SET SCHEMA``, which keeps its
        rows, constraints and indexes.

        Args:
            table_name: The table.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.
        """
        if (old_schema or None) == (new_schema or None):
            return
        qualified_table = self._qualify_table_name(table_name, old_schema)
        if new_schema:
            await self._run_sql(f"ALTER TABLE {qualified_table} SET SCHEMA {self.quote(new_schema)};")
            return
        table_literal = "'" + qualified_table.replace("'", "''") + "'"
        await self._run_sql(POSTGRESQL_MOVE_TABLE_TO_CURRENT_SCHEMA_TEMPLATE.format(table_literal=table_literal))

    async def _remake_table(self, model: type[Model], *args: Any, **kwargs: Any) -> None:
        """Rebuilds a table as ``TableRebuildMixin._remake_table()`` does, then sets its comment
        again - a PostgreSQL table comment belongs to the table, and went with the old one.

        Args:
            model: The model rendered from the state the table is rebuilt to.
            *args: The rebuild's other arguments.
            **kwargs: The rebuild's other keyword arguments.
        """
        await super()._remake_table(model, *args, **kwargs)
        if model._meta.table_description:
            await self.alter_table_comment(model)

    async def alter_table_comment(self, model: type[Model]) -> None:
        """Sets the table comment to the model's ``table_description`` (removes it when empty).

        Args:
            model: The model, rendered with its new description.
        """
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        if model._meta.table_description:
            await self._run_sql(
                self.TABLE_COMMENT_TEMPLATE.format(
                    table=qualified_table,
                    comment=self.client.dialect.get_string_literal_sql(model._meta.table_description),
                )
            )
        else:
            await self._run_sql(f"COMMENT ON TABLE {qualified_table} IS NULL;")

    async def _alter_column_comment(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        """Emit COMMENT ON COLUMN for PostgreSQL."""
        db_field = new_field.source_field or new_field.model_field_name
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        if new_field.description:
            comment = self.client.dialect.get_string_literal_sql(new_field.description)
            await self._run_sql(
                self.COLUMN_COMMENT_TEMPLATE.format(
                    table=qualified_table, column=self.quote(db_field), comment=comment
                )
            )
        else:
            # Remove comment: SET NULL
            await self._run_sql(f"COMMENT ON COLUMN {qualified_table}.{self.quote(db_field)} IS NULL;")

    def _format_index_type(self, index_type: str) -> str:
        return f"USING {index_type} "

    async def _get_unique_constraint_names_from_db(
        self, table_name: str, column_names: list[str], schema: str | None = None
    ) -> list[str]:
        """Query pg_constraint for unique constraint names matching exact column set."""
        # The names are bound as parameters, not put into the SQL text.
        query = (
            "SELECT con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "WHERE rel.relname = $1 "  # nosec B608
            "AND con.contype = 'u' "
            "AND nsp.nspname = COALESCE($2::text, current_schema()) "
            "AND ARRAY("
            "  SELECT att.attname::text"
            "  FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)"
            "  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum"
            "  ORDER BY k.ord"
            ") = $3::text[]"
        )
        _, rows = await self.client.execute(query, [table_name, schema, column_names])
        return [row["conname"] for row in rows]

    async def _get_composite_foreign_key_constraint_name_from_db(
        self, table_name: str, column_names: Sequence[str], schema: str | None = None
    ) -> str | None:
        """Same idea as _get_unique_constraint_names_from_db() above, for a composite (multi-
        column) FK constraint instead of a UniqueConstraint - matches con.contype = 'f' and the
        exact, ordered column set."""
        query = (
            "SELECT con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "WHERE rel.relname = $1 "  # nosec B608
            "AND con.contype = 'f' "
            "AND nsp.nspname = COALESCE($2::text, current_schema()) "
            "AND ARRAY("
            "  SELECT att.attname::text"
            "  FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)"
            "  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum"
            "  ORDER BY k.ord"
            ") = $3::text[]"
        )
        _, rows = await self.client.execute(query, [table_name, schema, list(column_names)])
        return rows[0]["conname"] if rows else None

    async def _get_foreign_key_constraint_name_from_db(
        self, table_name: str, column_name: str, schema: str | None = None
    ) -> str | None:
        """The name ``pg_constraint`` has for one column's foreign key - a column-level ``REFERENCES``
        clause gets a name PostgreSQL chose.
        """
        query = (
            "SELECT con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey) "
            "WHERE rel.relname = $1 "  # nosec B608
            "AND nsp.nspname = COALESCE($2::text, current_schema()) "
            "AND con.contype = 'f' "
            "AND att.attname = $3 "
            "AND cardinality(con.conkey) = 1"
        )
        _, rows = await self.client.execute(query, [table_name, schema, column_name])
        return rows[0]["conname"] if rows else None

    async def _drop_foreign_key_by_columns(
        self, table_name: str, column_names: Sequence[str], schema: str | None
    ) -> None:
        """Drops the FK constraint defined on exactly `column_names` of a table, if there is one.

        Args:
            table_name: The table owning the constraint.
            column_names: The constraint's own columns, in order.
            schema: The table's schema.
        """
        if len(column_names) == 1:
            constraint_name = await self._get_foreign_key_constraint_name_from_db(table_name, column_names[0], schema)
        else:
            constraint_name = await self._get_composite_foreign_key_constraint_name_from_db(
                table_name, column_names, schema
            )
        if constraint_name is None:
            return
        await self._run_sql(
            self.DELETE_CONSTRAINT_TEMPLATE.format(
                table=self._qualify_table_name(table_name, schema), name=self.quote(constraint_name)
            )
        )

    async def _drop_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        if isinstance(relation_field, ManyToManyFieldInstance):
            if relation_field._generated or relation_field.through_model is not None:
                return
            for side_keys in (relation_field.backward_keys, relation_field.forward_keys):
                await self._drop_foreign_key_by_columns(relation_field.through, side_keys, model._meta.schema)
            return
        if isinstance(relation_field, ForeignKeyFieldInstance):
            columns = [
                model._meta.fields_db_projection[key_field_name] for key_field_name in relation_field.source_fields
            ]
            await self._drop_foreign_key_by_columns(model._meta.db_table, columns, model._meta.schema)

    async def _rebuild_m2m_through_foreign_keys(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> None:
        """Drops and re-adds both sides' FK constraints of an auto-managed through table with the
        field's current ON DELETE action, keeping each existing constraint's name, and aligns the
        key columns' nullability with it (only ON DELETE SET NULL needs nullable columns)."""
        from hare.query.composite import KeyColumns

        schema = model._meta.schema
        through_table = field.through
        qualified_through = self._qualify_table_name(through_table, schema)
        nullability_template = (
            self.ALTER_FIELD_NULL_TEMPLATE
            if field.db_on_delete == OnDelete.SET_NULL
            else self.ALTER_FIELD_NOT_NULL_TEMPLATE
        )
        sides = ((field.backward_keys, model._meta), (field.forward_keys, field.related_model._meta))
        for side_keys, target_meta in sides:
            if len(side_keys) == 1:
                constraint_name = await self._get_foreign_key_constraint_name_from_db(
                    through_table, side_keys[0], schema
                )
            else:
                constraint_name = await self._get_composite_foreign_key_constraint_name_from_db(
                    through_table, side_keys, schema
                )
            if constraint_name is not None:
                await self._run_sql(
                    self.DELETE_CONSTRAINT_TEMPLATE.format(table=qualified_through, name=self.quote(constraint_name))
                )
            for key in side_keys:
                await self._run_sql(
                    self.ALTER_FIELD_TEMPLATE.format(
                        table=qualified_through, changes=nullability_template.format(column=self.quote(key))
                    )
                )
            if not self.creates_foreign_key(field):
                continue
            pk_columns = KeyColumns.get_source_columns(target_meta)
            if constraint_name is None:
                constraint_name = (
                    GeneratedNames.get_foreign_key_name(
                        through_table, (side_keys[0],), target_meta.db_table, (pk_columns[0],)
                    )
                    if len(side_keys) == 1
                    else GeneratedNames.get_foreign_key_name(
                        through_table, side_keys, target_meta.db_table, pk_columns
                    )
                )
            constraint_sql = self.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.quote(constraint_name),
                fields=", ".join(self.quote(key) for key in side_keys),
                table=self._qualify_table_name(target_meta.db_table, target_meta.schema),
                to_fields=", ".join(self.quote(column) for column in pk_columns),
                on_delete=field.db_on_delete,
            )
            await self._run_sql(
                self.ADD_CONSTRAINT_TEMPLATE.format(table=qualified_through, constraint=constraint_sql)
            )

    async def _alter_fk_on_delete(
        self,
        model: type[Model],
        db_field: str,
        old_fk_field: ForeignKeyFieldInstance[Model],
        new_fk_field: ForeignKeyFieldInstance[Model],
    ) -> None:
        table_name = model._meta.db_table
        schema = model._meta.schema
        if len(new_fk_field.source_fields) > 1:
            # Composite target: db_field is only the FIRST shadow column (see alter_field()'s
            # own call site comment) - the real constraint spans all of them, so it has to be
            # looked up and rebuilt by the full column set, not db_field alone.
            constraint_name = await self._get_composite_foreign_key_constraint_name_from_db(
                table_name, new_fk_field.source_fields, schema
            )
            if constraint_name is not None:
                await self._run_sql(
                    self.DELETE_CONSTRAINT_TEMPLATE.format(
                        table=self._qualify_table_name(table_name, schema), name=self.quote(constraint_name)
                    )
                )
            if not self.creates_foreign_key(new_fk_field):
                # db_constraint=False: the constraint dropped above isn't created again.
                return
            new_composite_constraint = self._get_composite_fk_constraint(model, new_fk_field)
            if constraint_name is not None:
                # Keeps the constraint's existing name, which may not follow today's naming.
                new_composite_constraint = ForeignKeyConstraint(
                    fields=new_composite_constraint.fields,
                    to_table=new_composite_constraint.to_table,
                    to_fields=new_composite_constraint.to_fields,
                    on_delete=new_composite_constraint.on_delete,
                    name=constraint_name,
                )
            await self.add_constraint(model, new_composite_constraint)
            return
        constraint_name = await self._get_foreign_key_constraint_name_from_db(table_name, db_field, schema)
        qualified_table = self._qualify_table_name(table_name, schema)
        if constraint_name is not None:
            await self._run_sql(
                self.DELETE_CONSTRAINT_TEMPLATE.format(table=qualified_table, name=self.quote(constraint_name))
            )
        if not self.creates_foreign_key(new_fk_field):
            # Same reasoning as the composite branch above: db_constraint=False on the new field
            # means no FK constraint at all - nothing more to (re)build once the old one (if any)
            # is dropped.
            return
        related_model = new_fk_field.related_model
        to_field_name = new_fk_field.to_field_instance.source_field or new_fk_field.to_field_instance.model_field_name
        if constraint_name is None:
            # No constraint existed to reuse the name of (old field had db_constraint=False) -
            # generate a fresh one, the same naming convention a brand-new column's own inline FK
            # reference uses (_get_fk_field_definition's identical call).
            constraint_name = GeneratedNames.get_foreign_key_name(
                table_name, (db_field,), related_model._meta.db_table, (to_field_name,)
            )
        new_constraint = (
            f"CONSTRAINT {self.quote(constraint_name)} FOREIGN KEY ({self.quote(db_field)}) "
            f"REFERENCES {self._qualify_table_name(related_model._meta.db_table, related_model._meta.schema)} "
            f"({self.quote(to_field_name)}) ON DELETE {new_fk_field.db_on_delete}"
        )
        await self._run_sql(self.ADD_CONSTRAINT_TEMPLATE.format(table=qualified_table, constraint=new_constraint))

    async def delete_model(self, model: type[Model]) -> None:
        """Drops the table, then its triggers' functions - a function isn't owned by the table, so
        it outlives DROP TABLE unless dropped too."""
        await super().delete_model(model)
        for trigger in model._meta.triggers:
            qualified_function = self._qualify_table_name(trigger.function_name, model._meta.schema)
            await self._run_sql(self.TRIGGER_FUNCTION_DROP_TEMPLATE.format(function_name=qualified_function))

    def get_trigger_create_sqls(self, model: type[Model], trigger: Trigger, safe: bool = False) -> list[str]:
        """The statements creating `trigger`'s backing function and the trigger itself - shared
        by add_trigger() and generate_schemas().

        Args:
            model: The model the trigger is declared on.
            trigger: The trigger.
            safe: Replace an existing function and trigger of the same name instead of failing.

        Returns:
            The DDL statements, in execution order.
        """
        table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        # The function lives in the model's schema, like its table - same-named functions of several
        # schemas don't collide, and dropping the schema drops it.
        qualified_function = self._qualify_table_name(trigger.function_name, model._meta.schema)
        function_template = (
            self.TRIGGER_FUNCTION_CREATE_OR_REPLACE_TEMPLATE if safe else self.TRIGGER_FUNCTION_CREATE_TEMPLATE
        )
        statements = [
            function_template.format(
                function_name=qualified_function,
                body=trigger.body,
                language=trigger.language or POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE,
            )
        ]
        if safe:
            statements.append(
                self.TRIGGER_DROP_IF_EXISTS_TEMPLATE.format(trigger_name=self.quote(trigger.name), table=table)
            )
        when_clause = f"\n    WHEN ({trigger.when})" if trigger.when else ""
        if trigger.deferrable:
            statements.append(
                self.TRIGGER_CONSTRAINT_CREATE_TEMPLATE.format(
                    trigger_name=self.quote(trigger.name),
                    timing=trigger.timing,
                    on=trigger.on,
                    table=table,
                    initially="DEFERRED" if trigger.initially_deferred else "IMMEDIATE",
                    for_each=trigger.for_each,
                    when_clause=when_clause,
                    function_name=qualified_function,
                )
            )
        else:
            statements.append(
                self.TRIGGER_CREATE_TEMPLATE.format(
                    trigger_name=self.quote(trigger.name),
                    timing=trigger.timing,
                    on=trigger.on,
                    table=table,
                    for_each=trigger.for_each,
                    when_clause=when_clause,
                    function_name=qualified_function,
                )
            )
        return statements

    async def add_trigger(self, model: type[Model], trigger: Trigger) -> None:
        for statement in self.get_trigger_create_sqls(model, trigger):
            await self._run_sql(statement)

    async def remove_trigger(self, model: type[Model], trigger: Trigger) -> None:
        table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        qualified_function = self._qualify_table_name(trigger.function_name, model._meta.schema)
        await self._run_sql(self.TRIGGER_DROP_TEMPLATE.format(trigger_name=self.quote(trigger.name), table=table))
        await self._run_sql(self.TRIGGER_FUNCTION_DROP_TEMPLATE.format(function_name=qualified_function))

    async def alter_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        # PostgreSQL can't alter a trigger's timing, events or body - it is dropped and created
        # again.
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)

    async def rename_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        if old_trigger.name == new_trigger.name:
            return
        if self.RENAME_TRIGGER_TEMPLATE and self.RENAME_TRIGGER_FUNCTION_TEMPLATE:
            table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
            await self._run_sql(
                self.RENAME_TRIGGER_TEMPLATE.format(
                    table=table,
                    old_name=self.quote(old_trigger.name),
                    new_name=self.quote(new_trigger.name),
                )
            )
            # A renamed trigger's backing function is renamed to match, keeping
            # Trigger.function_name's f"{name}_fn" convention consistent - RENAME TO takes just
            # the new bare name, a function can't change schema this way (nor does it need to).
            qualified_old_function = self._qualify_table_name(old_trigger.function_name, model._meta.schema)
            await self._run_sql(
                self.RENAME_TRIGGER_FUNCTION_TEMPLATE.format(
                    function_name=qualified_old_function,
                    new_function_name=self.quote(new_trigger.function_name),
                )
            )
            return
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)
