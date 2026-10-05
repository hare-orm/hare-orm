from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.conditions.tenant_condition import TenantCondition
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError, FieldError, QueryError, UnSupportedError
from hare.query.enums import LookupTarget

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.expressions import Q
    from hare.sql import Table


class ConstraintCondition:
    """The SQL a constraint's or partial index's condition is written into DDL as - a
    ``RawSQLTerm`` as given, a ``Q`` against the model's own columns with its values inline, an
    ``ExclusiveArcCondition`` as the dialect writes it."""

    @staticmethod
    def raise_if_not_condition(condition: Any, owner: str) -> None:
        """Rejects a condition that is neither a ``Q``, a ``RawSQLTerm`` nor an ``ExclusiveArcCondition``.

        Args:
            condition: The declared condition.
            owner: What declares it, named in the error - ``CheckConstraint.check``, ...

        Raises:
            ConfigurationError: The condition is something else - raw SQL text is
                ``RawSQLTerm("...")``.
        """
        # Local import: the query package imports the ddl package.
        from hare.query.expressions import Q

        if not isinstance(condition, (Q, RawSQLTerm, ExclusiveArcCondition)):
            raise ConfigurationError(
                f"{owner} takes a Q over the model's fields or RawSQLTerm(...) of raw SQL, got {condition!r}"
            )

    @staticmethod
    def get_sql(
        condition: Q | RawSQLTerm | ExclusiveArcCondition | TenantCondition,
        model: type[Model],
        client: DatabaseClient,
        *,
        table: Table | None = None,
    ) -> str:
        """Renders a condition for a table of ``model``.

        Args:
            condition: Raw SQL, a ``Q`` over the model's own fields, an exclusive arc, or a policy's tenant
                condition.
            model: The model whose table the constraint or index belongs to.
            client: The client of the database the DDL runs on.
            table: The table a ``Q`` reads the columns of - the model's own table, unqualified, when None.

        Returns:
            The SQL predicate.

        Raises:
            ConfigurationError: The ``Q`` is empty, or reads another model's fields or an aggregate.
            UnSupportedError: It needs a function the dialect only has on hare's own connections
                (a SQLite UDF), which a constraint or index in the database can't call.
        """
        if isinstance(condition, RawSQLTerm):
            return condition.sql
        if isinstance(condition, (ExclusiveArcCondition, TenantCondition)):
            return condition.get_condition_sql(model, client)
        # Local imports: hare.ddl is imported by the field and model modules the query package
        # itself depends on.
        from hare.query.expressions.expression_context import ExpressionContext
        from hare.sql import Table as SqlTable
        from hare.sql.exceptions import FunctionException

        if not condition:
            raise ConfigurationError(f"{model.__name__}: a constraint or index condition can't be empty")
        if condition.expression is not None or any(
            not ConstraintCondition.reads_own_column(model, key)
            for key in ConstraintCondition.get_lookup_keys(condition)
        ):
            raise ConfigurationError(
                f"{model.__name__}: a constraint or index condition reads only the model's own columns, "
                f"got {condition!r}"
            )
        meta = model._meta
        modifier = condition.get_result(
            ExpressionContext(
                model=model,
                table=table if table is not None else SqlTable(meta.db_table),
                annotations={},
                dialect=client.dialect,
                connection=None,
            )
        )
        if modifier.joins or modifier.having_criterion:
            raise ConfigurationError(
                f"{model.__name__}: a constraint or index condition reads only the model's own columns, "
                f"got {condition!r}"
            )
        context = client.query_class.SQL_CONTEXT.copy(native_functions_only=True)
        try:
            sql = modifier.where_criterion.get_sql(context)
        except FunctionException as error:
            raise ConfigurationError(f"{model.__name__}: {condition!r} can't be written into DDL - {error}") from None
        if (own_function := context.dialect.renderers.get_connection_only_function(sql)) is not None:
            raise UnSupportedError(
                f"{model.__name__}: {condition!r} needs {own_function}, a function {context.dialect} only has on "
                "hare's own connections - it can't be part of a constraint or index"
            )
        return sql

    @staticmethod
    def get_lookup_keys(condition: Q) -> list[str]:
        """Every lookup key of a ``Q`` tree."""
        return [
            *condition.filters,
            *(key for child in condition.children for key in ConstraintCondition.get_lookup_keys(child)),
        ]

    @staticmethod
    def reads_own_column(model: type[Model], key: str) -> bool:
        """Whether a filter key compares one of the model's own columns.

        Args:
            model: The model.
            key: The filter key.

        Returns:
            True for a lookup of a field stored in the model's own table.
        """
        field_name = key.partition("__")[0]
        field = model._meta.fields_map.get(field_name) if field_name != "pk" else model._meta.pk
        if field is None or not field.has_db_field:
            return False
        try:
            lookup_info = model._meta._get_lookup_info(key)
        except (FieldError, QueryError):
            return False
        return not lookup_info.relations and lookup_info.target == LookupTarget.FIELD
