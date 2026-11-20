from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.models.instances.field_snapshot import FieldSnapshot

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_mapping import ComparisonKeyReader


class PostgresqlComparisonKeys:
    """Keys for matching decoded values with PostgreSQL equality."""

    nan_key: ClassVar[object] = object()

    @classmethod
    async def prepare_float(cls, connection: DatabaseClient, values: Iterable[Any]) -> ComparisonKeyReader:
        """PostgreSQL groups all NaNs as equal."""
        return cls.get_float_key

    @classmethod
    def get_float_key(cls, value: Any) -> Any:
        return cls.nan_key if FieldSnapshot.is_nan(value) else value

    @staticmethod
    async def prepare_citext(connection: DatabaseClient, values: Iterable[Any]) -> ComparisonKeyReader:
        """Group spellings using the database's own CITEXT comparison."""
        spellings = list(dict.fromkeys(value for value in values if value is not None))
        equivalents: dict[str, str] = {}
        if len(spellings) > 1:
            # The type name is a constant. The spellings are the $1 parameter.
            rows = await connection.execute_dicts(
                "SELECT array_agg(ordinality ORDER BY ordinality) AS members "  # nosec B608
                f"FROM unnest(CAST($1 AS {CitextField.SQL_TYPE}[])) WITH ORDINALITY AS keys(value, ordinality) "
                "GROUP BY value",
                [spellings],
            )
            for row in rows:
                members = row["members"]
                representative = spellings[members[0] - 1]
                for ordinal in members:
                    equivalents[spellings[ordinal - 1]] = representative
        return lambda value: equivalents.get(value, value)
