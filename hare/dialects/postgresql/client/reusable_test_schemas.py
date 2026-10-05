from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from hare.dialects.postgresql.client.constants import (
    POSTGRES_CATALOG_CHANGED_SQL,
    POSTGRES_CATALOG_ROW_COUNT_EXPRESSION,
    POSTGRES_CLASS_DEFINITIONS_FINGERPRINT_EXPRESSION,
    POSTGRES_COMMENT_ON_DATABASE_SQL,
    POSTGRES_CURRENT_TRANSACTION_ID_EXPRESSION,
    POSTGRES_DATA_BEYOND_TABLES_SQL,
    POSTGRES_DATABASE_CATALOGS_SQL,
    POSTGRES_DATABASE_COMMENT_SQL,
    POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX,
    POSTGRES_RESTART_SEQUENCE_SQL,
    POSTGRES_STORAGE_WRITTEN_CATALOG,
    POSTGRES_USED_SEQUENCES_SQL,
    POSTGRES_USER_TABLES_SQL,
)

if TYPE_CHECKING:
    from hare.contrib.test.databases.kept_test_schema import KeptTestSchema
    from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient


class ReusableTestSchemas:
    """A reusable test database keeping the schema its lease created: when the test changed no part of
    it, only the rows are cleared, and the next lease asking for the same schema skips creating it -
    creating a test schema of hundreds of tables and dropping it again cost the server most of a
    suite's time.

    A change of the schema is told by the database's catalogs: a DDL statement adding or changing an
    object writes a catalog row, which carries the id of the transaction that wrote it, and one only
    dropping an object leaves its catalogs with fewer rows; rows written to the tables, ANALYZE and
    VACUUM write none.
    """

    @staticmethod
    def get_schema_key(schema_sql: str) -> str:
        """The digest of the DDL creating a schema - two leases asking for the same DDL get one schema.

        Args:
            schema_sql: The DDL.

        Returns:
            Its SHA-256, in hex.
        """
        return hashlib.sha256(schema_sql.encode()).hexdigest()

    @staticmethod
    async def get_catalog_names(client: PostgresqlClient) -> list[str]:
        """The catalogs of the database that DDL writes, by name.

        Args:
            client: The client of the database.

        Returns:
            The names, sorted.
        """
        return [row["catalog_name"] for row in await client.execute_dicts(POSTGRES_DATABASE_CATALOGS_SQL)]

    @classmethod
    async def get_kept_schema(cls, client: PostgresqlClient, schema_key: str) -> KeptTestSchema:
        """The state of a database's catalogs, to tell a later change of the schema by.

        Args:
            client: The client of the database, on a session no other one runs beside.
            schema_key: The digest of the DDL that created the schema.

        Returns:
            The schema with a transaction id later than every catalog row written so far and the row
            count of every catalog.
        """
        # Local import: the kept schema belongs to the test helpers.
        from hare.contrib.test.databases.kept_test_schema import KeptTestSchema

        catalog_names = await cls.get_catalog_names(client)
        counts = ", ".join(
            f"{POSTGRES_CATALOG_ROW_COUNT_EXPRESSION.format(catalog=name)} AS catalog_{number}"
            for number, name in enumerate(catalog_names)
        )
        sql = (
            f"SELECT {POSTGRES_CURRENT_TRANSACTION_ID_EXPRESSION} AS transaction_id, "  # nosec B608 - constants only
            f"{POSTGRES_CLASS_DEFINITIONS_FINGERPRINT_EXPRESSION} AS class_definitions_fingerprint, {counts}"
        )
        row = (await client.execute_dicts(sql))[0]
        return KeptTestSchema(
            schema_key,
            int(row["transaction_id"]),
            tuple((name, int(row[f"catalog_{number}"])) for number, name in enumerate(catalog_names)),
            row["class_definitions_fingerprint"],
        )

    @staticmethod
    async def write_kept_schema(client: PostgresqlClient, kept_schema: KeptTestSchema) -> None:
        """Keeps the schema in the database's comment, for a later test process to take - the comment
        is a row of a catalog the cluster shares, no change of the database's schema.

        Args:
            client: The client of the database.
            kept_schema: The schema the database holds.
        """
        literals = client.dialect.literals
        await client.execute_script(
            POSTGRES_COMMENT_ON_DATABASE_SQL.format(
                database=literals.quote_identifier(client.database or ""),
                comment=literals.get_string_literal_sql(
                    POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX + kept_schema.to_text()
                ),
            )
        )

    @staticmethod
    async def read_kept_schema(client: PostgresqlClient) -> KeptTestSchema | None:
        """The schema ``write_kept_schema()`` kept in the database's comment.

        Args:
            client: The client of the database.

        Returns:
            The schema, or None when the comment keeps none.
        """
        # Local import: the kept schema belongs to the test helpers.
        from hare.contrib.test.databases.kept_test_schema import KeptTestSchema

        rows = await client.execute_dicts(POSTGRES_DATABASE_COMMENT_SQL)
        comment = rows[0]["comment"] if rows else None
        if not comment or not comment.startswith(POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX):
            return None
        return KeptTestSchema.from_text(comment.removeprefix(POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX))

    @classmethod
    async def has_schema_changed(cls, client: PostgresqlClient, kept_schema: KeptTestSchema) -> bool:
        """Whether DDL ran on the database since its schema was kept: a catalog holds a row written
        later, or a different number of rows, or a relation's definition differs.

        Args:
            client: The client of the database, on a session no other one runs beside.
            kept_schema: The schema as kept.

        Returns:
            True when the schema changed.
        """
        catalog_names = await cls.get_catalog_names(client)
        if tuple(catalog_names) != tuple(name for name, _ in kept_schema.catalog_row_counts):
            return True
        conditions = " OR ".join(
            POSTGRES_CATALOG_CHANGED_SQL.format(catalog=name, transaction_id=int(kept_schema.catalog_transaction_id))
            for name in catalog_names
            if name != POSTGRES_STORAGE_WRITTEN_CATALOG
        )
        counts = ", ".join(
            f"{POSTGRES_CATALOG_ROW_COUNT_EXPRESSION.format(catalog=name)} AS catalog_{number}"
            for number, name in enumerate(catalog_names)
        )
        sql = (
            f"SELECT {conditions} AS changed, "  # nosec B608 - constants only
            f"{POSTGRES_CLASS_DEFINITIONS_FINGERPRINT_EXPRESSION} AS class_definitions_fingerprint, {counts}"
        )
        row = (await client.execute_dicts(sql))[0]
        if row["changed"] or row["class_definitions_fingerprint"] != kept_schema.class_definitions_fingerprint:
            return True
        return any(
            int(row[f"catalog_{number}"]) != count for number, (_, count) in enumerate(kept_schema.catalog_row_counts)
        )

    @classmethod
    async def clear_rows(cls, client: PostgresqlClient, kept_schema: KeptTestSchema) -> KeptTestSchema | None:
        """Empties the database's tables and restarts its sequences, keeping its schema - when the schema
        is the one kept and every row of the database lives in a table.

        Args:
            client: The client of the database, on a session no other one runs beside, its prepared
                transactions rolled back.
            kept_schema: The schema the database held when its lease began.

        Returns:
            The schema the database keeps now, or None when it has to be reset instead.
        """
        # Local import: the table clearing belongs to the schema editor, which imports the client's modules.
        from hare.dialects.postgresql.schema.runtime_statements.postgresql_table_clearing import (
            PostgresqlTableClearing,
        )

        if await cls.has_schema_changed(client, kept_schema):
            return None
        if (await client.execute_dicts(POSTGRES_DATA_BEYOND_TABLES_SQL))[0]["found"]:
            return None
        tables = [row["table_name"] for row in await client.execute_dicts(POSTGRES_USER_TABLES_SQL)]
        await PostgresqlTableClearing.clear_tables(client, tables)
        sequences = [row["sequence_name"] for row in await client.execute_dicts(POSTGRES_USED_SEQUENCES_SQL)]
        if sequences:
            await client.execute_script(
                ";\n".join(POSTGRES_RESTART_SEQUENCE_SQL.format(sequence=sequence) for sequence in sequences)
            )
        return await cls.get_kept_schema(client, kept_schema.schema_key)
