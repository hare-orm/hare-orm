from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.ddl_term_sql import DdlTermSql
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError
from hare.query.expressions import Expression, ExpressionContext, F
from hare.sql.terms.field import Field as HareSqlField

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.sql.terms.term import Term

#: What a declaration writes into DDL in place of raw SQL: a field's name, an expression of hare or raw SQL.
DeclaredExpression = str | Expression | RawSQLTerm


class DeclaredExpressions:
    """What a declaration writes into DDL - a field's name (its column), an expression of hare (``F``, a
    function, arithmetic) resolved against the model, or a ``RawSQLTerm`` as it is - in the SQL of the
    dialect the DDL runs on: one declaration, each database's own SQL."""

    @staticmethod
    def is_declared_expression(value: Any) -> bool:
        """Whether a value is one a declaration takes as an expression.

        Args:
            value: The value.

        Returns:
            True for a field's name (an identifier), an expression of hare, or a ``RawSQLTerm`` of
            non-empty SQL.
        """
        if isinstance(value, str):
            return value.isidentifier()
        if isinstance(value, RawSQLTerm):
            return bool(value.sql.strip())
        return isinstance(value, Expression)

    @staticmethod
    def get_term(expression: str | Expression, model: type[Model], dialect: Dialect) -> Term:
        """An expression resolved against a model for a dialect.

        Args:
            expression: A field's name or an expression of hare.
            model: The model the declaration is of.
            dialect: The dialect the DDL runs on.

        Returns:
            The term - a field's unqualified column, as the database reads a declaration back.

        Raises:
            ConfigurationError: A name is no field of the model, or the expression reads a related model's
                column - a table's DDL reads its own columns only.
        """
        if isinstance(expression, str) or (type(expression) is F and expression.name in model._meta.fields_map):
            field_name = expression if isinstance(expression, str) else expression.name
            column_names = model._meta.get_column_names([field_name]) if field_name in model._meta.fields_map else []
            if len(column_names) != 1:
                raise ConfigurationError(f"{model.__name__}: {expression!r} names no field of one column")
            return HareSqlField(column_names[0])
        expression_context = ExpressionContext(
            model=model, table=model.get_table(), annotations={}, dialect=dialect, connection=None
        )
        result = expression.get_result(expression_context)
        if result.joins:
            raise ConfigurationError(
                f"{model.__name__}: {expression!r} reads a column of a related model, which a declaration of the "
                "table can't - name the model's own column of the relation (F('owner_id') for owner)"
            )
        return result.term

    @staticmethod
    def get_sql(expression: DeclaredExpression, model: type[Model], dialect: Dialect) -> str:
        """An expression's SQL in a dialect's DDL.

        Args:
            expression: A field's name, an expression of hare or a ``RawSQLTerm``.
            model: The model the declaration is of.
            dialect: The dialect the DDL runs on.

        Returns:
            The SQL - a ``RawSQLTerm``'s as written.
        """
        if isinstance(expression, RawSQLTerm):
            return expression.sql
        sql_context = dialect.sql_context.copy(native_functions_only=True)
        return DdlTermSql.get_sql(
            DeclaredExpressions.get_term(expression, model, dialect), sql_context, model, expression
        )

    @staticmethod
    def get_column_names(expression: DeclaredExpression, model: type[Model], dialect: Dialect) -> set[str] | None:
        """The columns an expression reads.

        Args:
            expression: A field's name, an expression of hare or a ``RawSQLTerm``.
            model: The model the declaration is of.
            dialect: The dialect the DDL runs on.

        Returns:
            The column names; None for raw SQL, whose columns hare doesn't read out of it.
        """
        if isinstance(expression, RawSQLTerm):
            return None
        term = DeclaredExpressions.get_term(expression, model, dialect)
        return {field.name for field in term.fields_()}
