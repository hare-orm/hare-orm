from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.conditions.dialect_condition import DialectCondition
from hare.ddl.conditions.enum_members_condition import EnumMembersCondition
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.conditions.messages import (
    ROW_CONDITION_OUTER_MESSAGE,
    ROW_CONDITION_QUERY_MESSAGE,
    ROW_CONDITION_RELATION_MESSAGE,
)
from hare.ddl.ddl_term_sql import DdlTermSql
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError, FieldError, QueryError

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
        """Check declaration dependencies before any dialect renders SQL.

        Forward relation comparisons read local key columns. Related column paths,
        aggregates, windows and query scopes cannot belong to a row constraint.
        Scalar functions are checked by the actual dialect when DDL is rendered.
        """
        # Local imports: model binding imports the DDL package before query setup.
        from hare.query.enums import LookupTarget
        from hare.query.expressions import Aggregate, Q, Window
        from hare.query.expressions.structure.expression_declaration import ExpressionDeclaration
        from hare.query.expressions.structure.expression_traversal import ExpressionTraversal
        from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
        from hare.query.queryset.query_specification import QuerySpecification

        if not isinstance(condition, Q):
            return
        try:
            for node in ExpressionTraversal.walk(condition):
                if isinstance(node, (Aggregate, Window, QuerySpecification)) or (
                    isinstance(node, ExpressionDeclaration) and node.structure.query_scope
                ):
                    raise QueryError(ROW_CONDITION_QUERY_MESSAGE)
            for reference in ExpressionTraversal.get_field_references(condition):
                if reference.outer_scopes:
                    raise QueryError(ROW_CONDITION_OUTER_MESSAGE)
                if not reference.lookup:
                    if LookupInfoBuilder.get_ordering_info(model, reference.path).relations:
                        raise QueryError(ROW_CONDITION_RELATION_MESSAGE)
                    continue
                lookup = LookupInfoBuilder.get_lookup_info(model, reference.path)
                if not lookup.relations:
                    continue
                local_relation = (
                    lookup.target is LookupTarget.RELATION
                    and len(lookup.relations) == 1
                    and lookup.relations[0].model_field_name
                    in model._meta.foreign_key_fields | model._meta.one_to_one_fields
                )
                if not local_relation:
                    raise QueryError(ROW_CONDITION_RELATION_MESSAGE)
        except (FieldError, QueryError) as error:
            raise ConfigurationError(
                f"{model.__name__}: a constraint or index condition reads only the model's own columns, "
                f"got {condition!r}"
            ) from error
