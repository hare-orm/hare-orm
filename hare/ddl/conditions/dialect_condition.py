from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.conditions.field_condition import FieldCondition

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class DialectCondition(FieldCondition):
    """A condition of DDL the dialect of the database writes for a table of a model - an exclusive arc,
    an enum's members, a tenant condition."""

    __slots__ = ()

    def get_condition_sql(self, model: type[Model], client: DatabaseClient) -> str:
        """The predicate written into a table of ``model``.

        Args:
            model: The model the constraint, the index or the policy belongs to.
            client: The client of the database the DDL runs on.

        Returns:
            The SQL predicate.
        """
        raise NotImplementedError
