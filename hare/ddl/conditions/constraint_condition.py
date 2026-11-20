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
    from hare.models.model_binding import ModelBinding
    from hare.query.expressions import Q
    from hare.sql import Table


class ConstraintCondition:
    """The SQL a constraint's or partial index's condition is written into DDL as - a
    ``RawSQLTerm`` as given, a ``Q`` against the model's own columns with its values inline, an
    ``ExclusiveArcCondition`` as the dialect writes it, an ``EnumMembersCondition`` as the members' values."""

    @staticmethod
    def get_table_conditions(binding: ModelBinding) -> list[Q | RawSQLTerm | DialectCondition | None]:
        """Collect conditions from constraints and partial indexes in declaration order."""
        # Local imports: these declarations use ConstraintCondition for validation.
        from hare.ddl.constraints.check_constraint import CheckConstraint
        from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
        from hare.ddl.constraints.unique_constraint import UniqueConstraint
        from hare.ddl.indexes.partial_index import PartialIndex

        conditions: list[Q | RawSQLTerm | DialectCondition | None] = []
        for constraint in binding.constraints:
            if isinstance(constraint, (UniqueConstraint, ExclusionConstraint)):
                conditions.append(constraint.condition)
            elif isinstance(constraint, CheckConstraint):
                conditions.append(constraint.check)
        conditions.extend(index.condition for index in binding.indexes if isinstance(index, PartialIndex))
        return conditions

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
        binding: ModelBinding,
        client: DatabaseClient,
        *,
        table: Table | None = None,
    ) -> str:
        """Renders a condition for a table of ``model``.

        Args:
            condition: Raw SQL, a ``Q`` over the model's own fields, or a condition the dialect writes -
                an exclusive arc, an enum's members, a policy's tenant condition.
            binding: The model whose table the constraint or index belongs to.
            client: The client of the database the DDL runs on.
            table: The table a ``Q`` reads the columns of - the model's own table, unqualified, when None.

        Returns:
            The SQL predicate.

        Raises:
            ConfigurationError: The ``Q`` is empty, or reads another model's fields or an aggregate.
            UnSupportedError: It needs a function the dialect only has on hare's own connections,
                which a constraint or index in the database can't call.
        """
        model = binding._model
        if isinstance(condition, RawSQLTerm):
            return condition.sql
        if isinstance(condition, DialectCondition):
            return condition.get_condition_sql(binding, client)
        # Local imports: hare.ddl is imported by the field and model modules the query package
        # itself depends on.
        from hare.query.expressions.expression_context import ExpressionContext
        from hare.sql import Table as SqlTable

        if not condition:
            raise ConfigurationError(f"{model.__name__}: a constraint or index condition can't be empty")
        ConstraintCondition.raise_if_reads_other_columns(condition, binding)
        modifier = condition.get_result(
            ExpressionContext(
                binding=binding,
                table=table if table is not None else SqlTable(binding.db_table),
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
    def raise_if_reads_other_columns(condition: Any, binding: ModelBinding) -> None:
        """Check declaration dependencies before any dialect renders SQL.

        Forward relation comparisons read local key columns. Related column paths,
        aggregates, windows and query scopes cannot belong to a row constraint.
        Scalar functions are checked by the actual dialect when DDL is rendered.
        """
        # Local imports: model binding imports the DDL package before query setup.
        model = binding._model
        from hare.query.enums import LookupTarget
        from hare.query.expressions import Aggregate, Q, Window
        from hare.query.expressions.structure.expression_declaration import ExpressionDeclaration
        from hare.query.expressions.structure.expression_traversal import ExpressionTraversal
        from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
        from hare.query.queryset.query_specification import QuerySpecification

        if not isinstance(condition, Q):
            return
        relation_names = binding.forward_key_relation_names
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
                    if LookupInfoBuilder.get_ordering_info(binding, reference.path).relations:
                        raise QueryError(ROW_CONDITION_RELATION_MESSAGE)
                    continue
                lookup = LookupInfoBuilder.get_lookup_info(binding, reference.path)
                if not lookup.relations:
                    continue
                local_relation = (
                    lookup.target is LookupTarget.RELATION
                    and len(lookup.relations) == 1
                    and lookup.relations[0].model_field_name in relation_names
                )
                if not local_relation:
                    raise QueryError(ROW_CONDITION_RELATION_MESSAGE)
        except (FieldError, QueryError) as error:
            raise ConfigurationError(
                f"{model.__name__}: a constraint or index condition reads only the model's own columns, "
                f"got {condition!r}"
            ) from error
