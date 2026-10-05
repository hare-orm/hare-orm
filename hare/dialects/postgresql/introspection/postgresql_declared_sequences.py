from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.dialects.postgresql.constants import POSTGRESQL_DECLARED_SEQUENCES_SQL
from hare.fields.data.constants import INT64_MAX, INT64_MIN

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlDeclaredSequences:
    """The sequences a model declares as PostgreSQL has them: a sequence the database lacks is left
    out, one of other settings or another owner comes with the database's - a setting left unset is
    compared with PostgreSQL's default for it."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        schema: str,
        table: str,
        sequences: Sequence[DatabaseSequence],
        column_to_field_name: Mapping[str, str],
    ) -> tuple[DatabaseSequence, ...]:
        """Returns the sequences of a model as the database has them.

        Args:
            connection: The connection.
            schema: The schema of the model.
            table: The model's table.
            sequences: The sequences the model declares.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            The sequences.
        """
        if not sequences:
            return ()
        rows = await connection.execute_dicts(
            POSTGRESQL_DECLARED_SEQUENCES_SQL, [schema, [sequence.name for sequence in sequences]]
        )
        rows_by_name = {row["name"]: row for row in rows}
        return tuple(
            cls.get_observed_sequence(sequence, rows_by_name[sequence.name], table, column_to_field_name)
            for sequence in sequences
            if sequence.name in rows_by_name
        )

    @staticmethod
    def get_observed_sequence(
        sequence: DatabaseSequence, row: dict[str, Any], table: str, column_to_field_name: Mapping[str, str]
    ) -> DatabaseSequence:
        """A declared sequence with the settings and the owner the database keeps, where they are
        others.

        Args:
            sequence: The declared sequence.
            row: The database's sequence.
            table: The model's table.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            The sequence itself when the database keeps it as declared.
        """
        ascending = sequence.increment > 0
        minimum = sequence.minimum if sequence.minimum is not None else (1 if ascending else INT64_MIN)
        maximum = sequence.maximum if sequence.maximum is not None else (INT64_MAX if ascending else -1)
        start = sequence.start if sequence.start is not None else (minimum if ascending else maximum)
        observed_owned_by = (
            column_to_field_name.get(row["owner_column"], row["owner_column"])
            if row["owner_column"] is not None and row["owner_table"] == table
            else None
        )
        changes: dict[str, Any] = {}
        for name, declared, observed in (
            ("start", start, row["start_value"]),
            ("increment", sequence.increment, row["increment_by"]),
            ("minimum", minimum, row["min_value"]),
            ("maximum", maximum, row["max_value"]),
            ("cycle", sequence.cycle, row["cycle"]),
            ("cache", sequence.cache, row["cache_size"]),
            ("owned_by", sequence.owned_by, observed_owned_by),
        ):
            if declared != observed:
                changes[name] = observed
        return dataclasses.replace(sequence, **changes) if changes else sequence
