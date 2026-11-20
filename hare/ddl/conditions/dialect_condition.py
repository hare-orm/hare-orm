from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.conditions.field_condition import FieldCondition

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding


class DialectCondition(FieldCondition):
    """A condition of DDL the dialect of the database writes for a table of a model - an exclusive arc,
    an enum's members, a tenant condition."""

    __slots__ = ()

    def get_condition_sql(self, binding: ModelBinding, client: DatabaseClient) -> str:
        """The predicate written into a table of ``binding``.

        Args:
            binding: The structural metadata the constraint, index or policy belongs to.
            client: The client of the database the DDL runs on.

        Returns:
            The SQL predicate.
        """
        raise NotImplementedError
