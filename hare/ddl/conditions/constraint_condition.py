from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.conditions.dialect_condition import DialectCondition
from hare.ddl.conditions.enum_members_condition import EnumMembersCondition
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.ddl_term_sql import DdlTermSql
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError, FieldError, QueryError
from hare.query.enums import LookupTarget

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.expressions import Q
    from hare.sql import Table


class ConstraintCondition:
    """The SQL a constraint's or partial index's condition is written into DDL as - a
    ``RawSQLTerm`` as given, a ``Q`` against the model's own columns with its values inline, an
    ``ExclusiveArcCondition`` as the dialect writes it, an ``EnumMembersCondition`` as the members' values."""

    @staticmethod
    def raise_if_not_condition(condition: Any, owner: str) -> None:
        """Rejects a condition that is neither a ``Q``, a ``RawSQLTerm``, an ``ExclusiveArcCondition`` nor an
        ``EnumMembersCondition``.

        Args:
            condition: The declared condition.
            owner: What declares it, named in the error - ``CheckConstraint.check``, ...

        Raises:
            ConfigurationError: The condition is something else - raw SQL text is
                ``RawSQLTerm("...")``.
        """
        # Local import: the query package imports the ddl package.
        from hare.query.expressions import Q

        if not isinstance(condition, (Q, RawSQLTerm, ExclusiveArcCondition, EnumMembersCondition)):
            raise ConfigurationError(
                f"{owner} takes a Q over the model's fields or RawSQLTerm(...) of raw SQL, got "
                f"{ValueDescriptions.get_shown_value(condition)}"
            )

    @staticmethod
    def get_sql(
        condition: Q | RawSQLTerm | DialectCondition,
        model: type[Model],
        client: DatabaseClient,
        *,
        table: Table | None = None,
    ) -> str:
        """Renders a condition for a table of ``model``.

        Args:
            condition: Raw SQL, a ``Q`` over the model's own fields, or a condition the dialect writes -
                an exclusive arc, an enum's members, a policy's tenant condition.
            model: The model whose table the constraint or index belongs to.
            client: The client of the database the DDL runs on.
            table: The table a ``Q`` reads the columns of - the model's own table, unqualified, when None.

        Returns:
            The SQL predicate.

        Raises:
            ConfigurationError: The ``Q`` is empty, or reads another model's fields or an aggregate.
            UnSupportedError: It needs a function the dialect only has on hare's own connections,
                which a constraint or index in the database can't call.
        """
        if isinstance(condition, RawSQLTerm):
            return condition.sql
        if isinstance(condition, DialectCondition):
            return condition.get_condition_sql(model, client)
        # Local imports: hare.ddl is imported by the field and model modules the query package
        # itself depends on.
        from hare.query.expressions.expression_context import ExpressionContext
        from hare.sql import Table as SqlTable

        if not condition:
            raise ConfigurationError(f"{model.__name__}: a constraint or index condition can't be empty")
        ConstraintCondition.raise_if_reads_other_columns(condition, model)
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
        return DdlTermSql.get_sql(modifier.where_criterion, context, model, condition)

    @staticmethod
    def raise_if_reads_other_columns(condition: Any, model: type[Model]) -> None:
        """Refuses a ``Q`` condition reading anything but the model's own columns - another model's field
        through a relation, an aggregate, an expression; a condition of another type isn't read.

        Args:
            condition: The declared condition.
            model: The model declaring it.

        Raises:
            ConfigurationError: The ``Q`` reads something else.
        """
        # Local import: the query package imports the ddl package.
        from hare.query.expressions import Q

        if not isinstance(condition, Q):
            return
        if condition.expression is not None or any(
            not ConstraintCondition.reads_model_column(model, key)
            for key in ConstraintCondition.get_lookup_keys(condition)
        ):
            raise ConfigurationError(
                f"{model.__name__}: a constraint or index condition reads only the model's own columns, "
                f"got {condition!r}"
            )

    @staticmethod
    def get_lookup_keys(condition: Q) -> list[str]:
        """Every lookup key of a ``Q`` tree."""
        return [
            *condition.filters,
            *(key for child in condition.children for key in ConstraintCondition.get_lookup_keys(child)),
        ]

    @staticmethod
    def reads_model_column(model: type[Model], key: str) -> bool:
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
