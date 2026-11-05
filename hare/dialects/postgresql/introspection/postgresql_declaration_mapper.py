from __future__ import annotations

import dataclasses
from collections import Counter
from typing import Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.enums import RowLevelSecurity
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.postgresql.constants import (
    POSTGRESQL_DOMAIN_COLUMN_TODO,
    POSTGRESQL_EXCLUSION_CONSTRAINT_TYPE,
    POSTGRESQL_VIRTUAL_GENERATED_COLUMN,
)
from hare.dialects.postgresql.introspection.postgresql_definitions import PostgresqlDefinitions
from hare.dialects.postgresql.introspection.postgresql_inspected_schema_objects import PostgresqlInspectedSchemaObjects
from hare.dialects.postgresql.partitioning.observed_partitioning import ObservedPartitioning
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.postgresql.types.postgresql_physical_type import PostgresqlPhysicalType
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import OnDelete
from hare.inspectdb.introspection.catalog_diagnostic import CatalogDiagnostic
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.enums import CatalogDiagnosticCode
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.sql.order import Order


class PostgresqlDeclarationMapper:
    """Reconstruct model declarations from a captured catalog without database IO.

    Parsing may leave unsupported objects as raw diagnostics. The source snapshot
    remains intact and can be mapped again or inspected independently of models.
    """

    def __init__(self, introspector: type[SchemaIntrospector]) -> None:
        self.introspector = introspector

    def get_tables(self, snapshot: CatalogSnapshot) -> list[TableInfo]:
        """Map each requested table, preserving request order and source records."""
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for section, rows in snapshot.copy_records().items():
            by_table: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                by_table.setdefault(row["table_name"], []).append(row)
            grouped[section] = by_table
        tables = []
        for table in snapshot.tables:
            info = self.build_table(
                table,
                snapshot.schema,
                table_row=grouped["tables"][table][0],
                primary_key_rows=grouped["primary_keys"].get(table, []),
                index_rows=grouped["indexes"].get(table, []),
                column_rows=grouped["columns"].get(table, []),
                foreign_key_rows=grouped["foreign_keys"].get(table, []),
                trigger_rows=grouped["triggers"].get(table, []),
                constraint_rows=grouped["constraints"].get(table, []),
                partition_rows=grouped["partitions"].get(table, []),
            )
            info.policies = PostgresqlInspectedSchemaObjects.get_policies(grouped["policies"].get(table, []))
            info.grants, unparsed_grants = PostgresqlInspectedSchemaObjects.get_grants(
                grouped["table_privileges"].get(table, []), grouped["column_privileges"].get(table, [])
            )
            info.diagnostics.extend(
                CatalogDiagnostic(CatalogDiagnosticCode.GRANT, "", note) for note in unparsed_grants
            )
            for row in grouped["sequences"].get(table, []):
                if row["declaring_table"] == table:
                    info.sequences.append(PostgresqlInspectedSchemaObjects.get_sequence(row, row["owner_column"]))
                else:
                    info.sequences_of_other_tables.append((row["name"], row["declaring_table"]))
            tables.append(info)
        return tables

    @staticmethod
    def get_table_options(
        table: str, table_row: dict[str, Any], partition_rows: list[dict[str, Any]]
    ) -> PostgresqlTableOptions | None:
        """Builds a table's ``PostgresqlTableOptions`` from its POSTGRESQL_TABLES_SQL row and, for a
        partitioned table, its POSTGRESQL_PARTITIONS_SQL rows: the storage parameters and the
        tablespace are those of its partitions - a value they don't share is named as such.

        Args:
            table: The table's name.
            table_row: The table's row.
            partition_rows: Its partition rows - none for a plain table.

        Returns:
            The options, None when every one has its default.
        """
        storage_parameters = PostgresqlDefinitions.get_storage_parameters(table_row["storage_parameters"])
        tablespace = table_row["tablespace"]
        partitions = [row for row in partition_rows if row["partition_table"] is not None]
        if partitions:
            storage_parameters = ObservedPartitioning.merge_partition_values(
                {
                    row["partition_table"]: PostgresqlDefinitions.get_storage_parameters(row["storage_parameters"])
                    for row in partitions
                }
            )
            default_tablespace = table_row["default_tablespace"]
            tablespaces = ObservedPartitioning.merge_partition_values(
                {
                    name: {"tablespace": partition_tablespace or default_tablespace}
                    for name, partition_tablespace in [
                        (table, tablespace),
                        *((row["partition_table"], row["tablespace"]) for row in partitions),
                    ]
                }
            )
            if tablespaces["tablespace"] != (tablespace or default_tablespace):
                tablespace = tablespaces["tablespace"]
        return PostgresqlTableOptions.from_observed(
            {
                "unlogged": bool(table_row["unlogged"]),
                "storage_parameters": storage_parameters,
                "tablespace": tablespace,
                "partitioning": ObservedPartitioning.build(table, partition_rows),
            }
        )

    @staticmethod
    def get_unparsed_partitioning(table: str, partition_rows: list[dict[str, Any]]) -> str | None:
        """How a table is partitioned when hare declares no such partitioning - a key over an expression,
        hash partitions of different moduli, a partition partitioned itself.

        Args:
            table: The table's name.
            partition_rows: Its POSTGRESQL_PARTITIONS_SQL rows - none for a plain table.

        Returns:
            ``PARTITION BY ...`` as PostgreSQL writes it, with the partitions partitioned themselves; None for
            a plain table and one ``PostgresqlTableOptions(partitioning=...)`` declares.
        """
        if not partition_rows:
            return None
        partitioned_partitions = [row["partition_table"] for row in partition_rows if row["partition_partitioned"]]
        if ObservedPartitioning.build(table, partition_rows) is not None and not partitioned_partitions:
            return None
        unparsed_partitioning = f"PARTITION BY {partition_rows[0]['key_definition']}"
        if partitioned_partitions:
            unparsed_partitioning += f", partitioned partitions {', '.join(partitioned_partitions)}"
        return unparsed_partitioning

    def build_table(
        self,
        table: str,
        schema: str,
        *,
        primary_key_rows: list[dict[str, Any]],
        index_rows: list[dict[str, Any]],
        column_rows: list[dict[str, Any]],
        foreign_key_rows: list[dict[str, Any]],
        table_row: dict[str, Any],
        trigger_rows: list[dict[str, Any]],
        constraint_rows: list[dict[str, Any]],
        partition_rows: list[dict[str, Any]],
    ) -> TableInfo:
        """Assembles one table's TableInfo from its rows of the batched catalog queries.

        Args:
            table: The table's name.
            schema: The schema it lives in.
            primary_key_rows: Its POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL rows.
            index_rows: Its POSTGRESQL_INDEXES_SQL rows.
            column_rows: Its POSTGRESQL_COLUMNS_SQL rows.
            foreign_key_rows: Its POSTGRESQL_FOREIGN_KEYS_SQL rows.
            table_row: Its POSTGRESQL_TABLES_SQL row - the comment and the storage.
            trigger_rows: Its POSTGRESQL_TRIGGERS_SQL rows.
            constraint_rows: Its POSTGRESQL_CONSTRAINTS_SQL rows.
            partition_rows: Its POSTGRESQL_PARTITIONS_SQL rows.

        Returns:
            The table's TableInfo.
        """
        indexes, column_indexes, unparsed_indexes, unique_columns, indexed_columns = (
            PostgresqlDeclarationMapper.build_indexes(index_rows)
        )
        columns = self.build_columns(column_rows, primary_key_rows, unique_columns, indexed_columns)
        foreign_keys, composite_foreign_keys, unparsed_foreign_keys = PostgresqlDeclarationMapper.build_foreign_keys(
            foreign_key_rows, schema
        )
        triggers, unparsed_triggers = PostgresqlDeclarationMapper.build_triggers(trigger_rows)
        (
            exclusion_constraints,
            unparsed_exclusion_constraints,
            check_constraints,
            unparsed_check_constraints,
            not_valid_constraint_names,
        ) = PostgresqlDeclarationMapper.build_constraints(constraint_rows)

        return TableInfo(
            name=table,
            diagnostics=[
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.TRIGGER, name, definition)
                    for name, definition in unparsed_triggers
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.EXCLUSION_CONSTRAINT, name, definition)
                    for name, definition in unparsed_exclusion_constraints
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.FOREIGN_KEY, name, columns=columns)
                    for name, columns in unparsed_foreign_keys
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.INDEX, name, definition)
                    for name, definition in unparsed_indexes
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.CHECK_CONSTRAINT, name, definition)
                    for name, definition in unparsed_check_constraints
                ),
                *(
                    [CatalogDiagnostic(CatalogDiagnosticCode.PARTITIONING, table, partitioning)]
                    if (partitioning := self.get_unparsed_partitioning(table, partition_rows)) is not None
                    else []
                ),
            ],
            schema=schema,
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=indexes,
            column_indexes=column_indexes,
            table_description=table_row["description"],
            triggers=triggers,
            exclusion_constraints=exclusion_constraints,
            composite_foreign_keys=composite_foreign_keys,
            check_constraints=check_constraints,
            not_valid_constraint_names=not_valid_constraint_names,
            is_in_default_schema=bool(table_row["is_default_schema"]),
            table_options=PostgresqlDeclarationMapper.get_table_options(table, table_row, partition_rows),
            row_level_security=PostgresqlDeclarationMapper.get_row_level_security(table_row),
            primary_key_without_overlaps=any(row["is_primary"] and row["without_overlaps"] for row in index_rows),
        )

    def get_column_db_default(self, column_default: str | None, is_generated_key: bool) -> Any:
        """A column's default - a sequence's next value only on a column other than the primary key of one
        column: a generated key takes its own sequence, another column (SERIAL, a column of a composite
        key) the declared one.

        Args:
            column_default: The default's SQL, None for none.
            is_generated_key: Whether the column is the table's primary key of one column.

        Returns:
            The default.
        """
        if not column_default:
            return None
        if not is_generated_key and column_default.strip().lower().startswith(
            self.introspector.SEQUENCE_DEFAULT_PREFIXES
        ):
            return SqlDefault(column_default.strip())
        return self.introspector.parse_db_default(column_default)

    @staticmethod
    def get_row_level_security(table_row: dict[str, Any]) -> RowLevelSecurity | None:
        """Whether row level security filters a table's rows, by its POSTGRESQL_TABLES_SQL row.

        Args:
            table_row: The row.

        Returns:
            ``FORCED`` when it filters the owner's rows too, ``ENABLED``, or None.
        """
        if not table_row["row_level_security"]:
            return None
        return RowLevelSecurity.FORCED if table_row["forced_row_level_security"] else RowLevelSecurity.ENABLED

    @staticmethod
    def build_indexes(
        index_rows: list[dict[str, Any]],
    ) -> tuple[list[IndexInfo], list[IndexInfo], list[tuple[str, str]], set[str], set[str]]:
        """Builds a table's indexes - each an index of its own, a single column's (``db_index=True``,
        ``unique=True``) or one hare can't rebuild. The primary key's own index is left out.

        Args:
            index_rows: The table's POSTGRESQL_INDEXES_SQL rows.

        Returns:
            The indexes, the single columns' indexes, the unparsed indexes' names and definitions,
            and the unique and the indexed single columns.
        """
        unique_columns: set[str] = set()
        indexed_columns: set[str] = set()
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        unparsed_indexes: list[tuple[str, str]] = []
        for row in index_rows:
            if row["is_primary"]:
                continue  # the primary key's own backing index, already covered by pk_columns
            storage_parameters = PostgresqlDefinitions.parse_storage_parameters(row["storage_parameters"])
            if None in row["columns"] or any(row["key_collations"]):
                # An index with an expression term is rebuilt as Index(*expressions) of raw SQL
                # terms. Not when a term also has a non-default opclass - that index stays unparsed.
                expression_terms = (
                    [
                        f"{term} COLLATE {SchemaIntrospector.quote_identifier(collation)}" if collation else term
                        for term, collation in zip(row["term_sqls"], row["key_collations"], strict=True)
                    ]
                    if all(row["opclass_is_default"])
                    else None
                )
                if expression_terms is None or any(not term for term in expression_terms):
                    unparsed_indexes.append((row["index_name"], row["index_def"]))
                    continue
                index_type = "" if row["index_type"] == "btree" else row["index_type"]
                indexes.append(
                    IndexInfo(
                        columns=[],
                        key_orders=PostgresqlDefinitions.get_key_orders(row),
                        is_unique=row["is_unique"],
                        name=row["index_name"],
                        index_type=index_type,
                        expression_terms=expression_terms,
                        storage_parameters=storage_parameters,
                        condition_sql=SchemaIntrospector.strip_outer_parentheses(row["condition_sql"]),
                        include=list(row["include_columns"] or []),
                        nulls_not_distinct=row["nulls_not_distinct"],
                        is_valid=row["is_valid"],
                    )
                )
                continue
            index_columns = list(row["columns"])
            index_type = "" if row["index_type"] == "btree" else row["index_type"]
            opclasses = list(row["opclasses"]) if not all(row["opclass_is_default"]) else []
            default_opclasses = list(row["opclasses"]) if not opclasses else []
            if row["without_overlaps"]:
                # A UNIQUE ... WITHOUT OVERLAPS constraint's GiST index and opclasses are its own.
                index_type, opclasses, default_opclasses = "", [], []
            # A single-column btree index with the default opclass and no condition becomes a field
            # flag; anything else needs Meta.indexes.
            key_orders = PostgresqlDefinitions.get_key_orders(row)
            is_simple = (
                all(key_order == Order.ASC_NULLS_LAST for key_order in key_orders)
                and len(index_columns) == 1
                and not index_type
                and not opclasses
                and not row["condition_sql"]
                and not row["include_columns"]
                and not row["nulls_not_distinct"]
                and not row["without_overlaps"]
                and row["is_valid"]
            )
            index_info = IndexInfo(
                columns=index_columns,
                is_unique=row["is_unique"],
                name=row["index_name"],
                index_type=index_type,
                opclasses=opclasses,
                default_opclasses=default_opclasses,
                storage_parameters=storage_parameters,
                deferrable=row["is_deferrable"],
                initially_deferred=row["is_initially_deferred"],
                condition_sql=SchemaIntrospector.strip_outer_parentheses(row["condition_sql"]),
                include=list(row["include_columns"] or []),
                nulls_not_distinct=row["nulls_not_distinct"],
                key_orders=key_orders,
                without_overlaps=row["without_overlaps"],
                is_valid=row["is_valid"],
            )
            if is_simple:
                if row["is_unique"]:
                    unique_columns.add(index_columns[0])
                else:
                    indexed_columns.add(index_columns[0])
                column_indexes.append(index_info)
            else:
                indexes.append(index_info)
        return indexes, column_indexes, unparsed_indexes, unique_columns, indexed_columns

    def build_columns(
        self,
        column_rows: list[dict[str, Any]],
        primary_key_rows: list[dict[str, Any]],
        unique_columns: set[str],
        indexed_columns: set[str],
    ) -> list[ColumnInfo]:
        """Builds a table's columns.

        Args:
            column_rows: The table's POSTGRESQL_COLUMNS_SQL rows.
            primary_key_rows: The table's POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL rows.
            unique_columns: The columns of a single-column unique index.
            indexed_columns: The columns of a single-column index.

        Returns:
            The columns.
        """
        pk_columns = {row["column_name"] for row in primary_key_rows}
        # The constraint's own declared column order, not physical table column order - see
        # ColumnInfo.pk_position.
        pk_positions = {row["column_name"]: row["ordinal_position"] for row in primary_key_rows}

        return [
            ColumnInfo(
                name=row["column_name"],
                db_type=row["data_type"],
                nullable=row["is_nullable"] == "YES",
                is_pk=row["column_name"] in pk_columns,
                pk_position=pk_positions.get(row["column_name"]),
                is_unique=row["column_name"] in unique_columns,
                has_index=row["column_name"] in indexed_columns,
                # Precision and scale are reported for every numeric type - used only once the
                # column is mapped to a DecimalField.
                max_length=row["character_maximum_length"],
                numeric_precision=row["numeric_precision"],
                numeric_scale=row["numeric_scale"],
                db_default=self.get_column_db_default(row["column_default"], pk_columns == {row["column_name"]}),
                description=row["description"],
                udt_name=row["udt_name"],
                # An array made with no declaration (CREATE TABLE AS) records none - one, as declared int[].
                array_dimensions=max(row["attndims"], 1) if row["data_type"] == "ARRAY" else 0,
                vector_dimensions=physical_type.vector_dimensions if row["udt_name"] == "vector" else None,
                array_element_numeric_precision_scale=(
                    physical_type.element.numeric_precision_scale if physical_type.element is not None else None
                ),
                array_element_max_length=(
                    physical_type.element.character_length if physical_type.element is not None else None
                ),
                # information_schema.columns reports a GENERATED ALWAYS AS (...) column's own
                # expression text directly, so parsing never fails here.
                generated_expression=row["generation_expression"] if row["is_generated"] == "ALWAYS" else None,
                generated_stored=row["attgenerated"] != POSTGRESQL_VIRTUAL_GENERATED_COLUMN,
                identity_generation=row["identity_generation"] if row["is_identity"] == "YES" else None,
                full_type=row["full_type"],
                enum_labels=tuple(row["enum_labels"]) if row["enum_labels"] is not None else None,
                enum_type_schema=row["enum_type_schema"],
                todo_reason=None
                if row["domain_name"] is None
                else POSTGRESQL_DOMAIN_COLUMN_TODO.format(domain=row["domain_name"], type=row["data_type"]),
                generates_key=row["is_identity"] == "YES"
                or (row["column_default"] or "")
                .strip()
                .lower()
                .startswith(self.introspector.SEQUENCE_DEFAULT_PREFIXES),
            )
            for row in column_rows
            for physical_type in (PostgresqlPhysicalType.from_sql(row["full_type"]),)
        ]

    @staticmethod
    def build_foreign_keys(
        foreign_key_rows: list[dict[str, Any]], schema: str
    ) -> tuple[dict[str, ForeignKeyInfo], list[CompositeForeignKeyInfo], list[tuple[str, tuple[str, ...]]]]:
        """Builds a table's foreign keys.

        Args:
            foreign_key_rows: The table's POSTGRESQL_FOREIGN_KEYS_SQL rows.
            schema: The schema the table lives in.

        Returns:
            The single-column foreign keys by column, the composite ones, and the names and columns
            of foreign keys hare can't declare, including overlapping single-column constraints.
        """
        # A composite FOREIGN KEY becomes one ForeignKeyField when it references the target's whole
        # primary key under the shadow column names; otherwise its columns stay plain fields and it
        # is listed as unparsed. Its columns never become single-column foreign keys.
        column_counts = Counter(column for row in foreign_key_rows for column in row["columns"])
        composite_foreign_keys: list[CompositeForeignKeyInfo] = []
        unparsed_foreign_keys: list[tuple[str, tuple[str, ...]]] = []
        for row in foreign_key_rows:
            member_columns = tuple(row["columns"])
            if any(column_counts[column] > 1 for column in member_columns):
                unparsed_foreign_keys.append((row["conname"], member_columns))
                continue
            if len(member_columns) <= 1:
                continue
            match = SchemaIntrospector.match_composite_foreign_key_naming(
                list(member_columns), list(row["target_columns"]), list(row["target_primary_key_columns"] or ())
            )
            # A relation can only name a model of this same run - one whose table is in this schema.
            if match is None or row["target_schema"] != schema:
                unparsed_foreign_keys.append((row["conname"], member_columns))
                continue
            field_name, ordered_columns = match
            composite_foreign_keys.append(
                CompositeForeignKeyInfo(
                    field_name=field_name,
                    columns=ordered_columns,
                    target_table=row["target_table"],
                    on_delete=(
                        OnDelete(row["delete_rule"]) if row["delete_rule"] in set(OnDelete) else OnDelete.CASCADE
                    ),
                    on_update=row["update_rule"],
                    deferral=SchemaIntrospector.get_deferral(row["is_deferrable"], row["is_initially_deferred"]),
                )
            )

        foreign_keys = {}
        for row in foreign_key_rows:
            if len(row["columns"]) != 1 or column_counts[row["columns"][0]] > 1:
                continue
            column_name = row["columns"][0]
            target_column = row["target_columns"][0]
            # ForeignKeyFieldInstance already defaults to the target's PK, so to_field= is only
            # needed when the real target column is something else, e.g. a UNIQUE column.
            target_pk_columns = list(row["target_primary_key_columns"] or ())
            foreign_keys[column_name] = ForeignKeyInfo(
                column=column_name,
                target_table=row["target_table"],
                target_column=target_column,
                on_delete=OnDelete(row["delete_rule"]) if row["delete_rule"] in set(OnDelete) else OnDelete.CASCADE,
                to_field=target_column if target_pk_columns != [target_column] else None,
                target_schema=row["target_schema"],
                on_update=row["update_rule"],
                deferral=SchemaIntrospector.get_deferral(row["is_deferrable"], row["is_initially_deferred"]),
            )
        return foreign_keys, composite_foreign_keys, unparsed_foreign_keys

    @staticmethod
    def build_triggers(trigger_rows: list[dict[str, Any]]) -> tuple[list[Trigger], list[tuple[str, str]]]:
        """Builds a table's triggers.

        Args:
            trigger_rows: The table's POSTGRESQL_TRIGGERS_SQL rows.

        Returns:
            The triggers, and the unparsed triggers' names and definitions.
        """
        # Only the trigger declaration needs parsing; its function attributes come from the catalog.
        triggers: list[Trigger] = []
        unparsed_triggers: list[tuple[str, str]] = []
        for row in trigger_rows:
            trigger = PostgresqlDefinitions.parse_trigger(
                row["name"], row["trigger_def"], row["body"], row["language"], row["library"]
            )
            if trigger is not None:
                triggers.append(trigger if row["enabled"] else dataclasses.replace(trigger, enabled=False))
            else:
                unparsed_triggers.append((row["name"], row["trigger_def"]))
        return triggers, unparsed_triggers

    @staticmethod
    def build_constraints(
        constraint_rows: list[dict[str, Any]],
    ) -> tuple[
        list[ExclusionConstraint], list[tuple[str, str]], list[CheckConstraint], list[tuple[str, str]], list[str]
    ]:
        """Builds a table's exclusion and CHECK constraints.

        Args:
            constraint_rows: The table's POSTGRESQL_CONSTRAINTS_SQL rows.

        Returns:
            The exclusion constraints, the unparsed ones' names and definitions, the CHECK
            constraints, the unparsed ones' names and definitions, and the names of the CHECK
            constraints added NOT VALID.
        """
        # pg_get_constraintdef()'s canonical text is regex-parsed, since the catalog splits an
        # exclusion constraint's per-column operators across several system columns with no single
        # structured view of "column WITH operator" pairs.
        exclusion_constraints: list[ExclusionConstraint] = []
        unparsed_exclusion_constraints: list[tuple[str, str]] = []
        check_constraints: list[CheckConstraint] = []
        unparsed_check_constraints: list[tuple[str, str]] = []
        not_valid_constraint_names: list[str] = []
        for row in constraint_rows:
            if row["constraint_type"] == POSTGRESQL_EXCLUSION_CONSTRAINT_TYPE:
                exclusion_constraint = PostgresqlDefinitions.parse_exclusion_constraint(
                    row["conname"], row["definition"]
                )
                if exclusion_constraint is not None:
                    exclusion_constraints.append(exclusion_constraint)
                else:
                    unparsed_exclusion_constraints.append((row["conname"], row["definition"]))
                continue
            check_constraint = PostgresqlDefinitions.parse_check_constraint(row["conname"], row["definition"])
            if check_constraint is not None:
                check_constraints.append(check_constraint)
                if row["definition"].rstrip().endswith(" NOT VALID"):
                    not_valid_constraint_names.append(row["conname"])
            else:
                unparsed_check_constraints.append((row["conname"], row["definition"]))
        return (
            exclusion_constraints,
            unparsed_exclusion_constraints,
            check_constraints,
            unparsed_check_constraints,
            not_valid_constraint_names,
        )
