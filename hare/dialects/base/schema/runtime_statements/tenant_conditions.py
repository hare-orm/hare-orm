from __future__ import annotations

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError


class TenantConditions(SchemaEditorPart):
    """Conditions of check constraints built from fields: a row belonging to the active tenant, exactly
    one branch of an exclusive arc set, every column of a key set or none."""

    __slots__ = ()

    @classmethod
    def get_tenant_condition_sql(cls, quoted_column: str, column_type: str) -> str:
        """The predicate of a policy's ``TenantCondition``: the row's tenant column holds one of the
        tenants the transaction set.

        Args:
            quoted_column: The quoted tenant column.
            column_type: The column's type.

        Returns:
            The SQL predicate.

        Raises:
            UnSupportedError: The database has no row level security - by default.
        """
        raise UnSupportedError("The database has no row level security - no TenantCondition policy")

    @classmethod
    def get_exclusive_arc_check_sql(cls, quoted_column_groups: list[list[str]], allow_none: bool) -> str:
        """The predicate of an exclusive arc's CHECK (``ExclusiveArcCondition``): one group of
        columns set - at most one with ``allow_none`` - and each group set or unset as a whole.

        Args:
            quoted_column_groups: The quoted key columns of each relation of the arc.
            allow_none: Whether no relation may be set.

        Returns:
            The SQL predicate.
        """
        set_count_sql = cls.get_set_column_count_sql([group[0] for group in quoted_column_groups])
        conditions = [f"{set_count_sql} {'<=' if allow_none else '='} 1"]
        conditions.extend(cls.get_all_or_none_set_sql(group) for group in quoted_column_groups if len(group) > 1)
        return " AND ".join(conditions)

    @classmethod
    def get_set_column_count_sql(cls, quoted_columns: list[str]) -> str:
        """How many of the columns are not NULL.

        Args:
            quoted_columns: The quoted columns.

        Returns:
            The SQL expression.
        """
        return "(" + " + ".join(f"CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END" for column in quoted_columns) + ")"

    @classmethod
    def get_all_or_none_set_sql(cls, quoted_columns: list[str]) -> str:
        """Whether the columns are all NULL or none of them is.

        Args:
            quoted_columns: The quoted columns.

        Returns:
            The SQL predicate.
        """
        all_null = " AND ".join(f"{column} IS NULL" for column in quoted_columns)
        none_null = " AND ".join(f"{column} IS NOT NULL" for column in quoted_columns)
        return f"(({all_null}) OR ({none_null}))"
