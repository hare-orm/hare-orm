from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import QueryError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.key_columns import KeyColumns
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers
from hare.sql.terms.star import Star

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.expressions import Q
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
    from hare.sql.builder.tables.table import Table
    from hare.sql.terms.criteria.criterion import Criterion
    from hare.sql.terms.subqueries.exists_term import ExistsTerm


class ScopeJoinConditions:
    """Conditions for relation joins and the links of a many-to-many relation."""

    @staticmethod
    def get_through_target_exists(
        through_table: Table,
        target_table: Table,
        forward_keys: tuple[str, ...],
        target_binding: ModelBinding,
        target_criterion: Criterion,
    ) -> ExistsTerm:
        """Match the link's columns to the target primary key in key order."""
        from hare.sql.terms.subqueries.exists_term import ExistsTerm

        link = KeyColumns.row_equality(
            [through_table[column] for column in forward_keys],
            [target_table[column] for column in KeyColumns.get_source_columns(target_binding)],
        )
        return ExistsTerm(QueryBuilder().from_(target_table).select(Star()).where(link & target_criterion))

    @staticmethod
    def fold_ambient_scope_into_join(
        joins: list[TableCriterionTuple],
        related_binding: ModelBinding,
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
        application_context: HareContext | None = None,
        position: int = -1,
    ) -> None:
        """ANDs the related model's default scope into ``joins[position]``, in place - a JOIN never goes
        through the model's manager.

        Args:
            joins: The ordered join pairs.
            related_binding: The model binding of the selected join.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
            application_context: The application context used to bind values and check model routing.
            position: The join's position, the last by default.
        """
        # Local import: hare.query.managers.manager imports QuerySet, which eventually imports this module
        # at module level - importing it back here at module level would be circular.
        from hare.query.scopes.row_scopes import RowScopes

        if connection is not None and related_binding.checks_tenant_client:
            related_binding.check_tenant_client(connection)
        join_table, join_criterion = joins[position]
        ambient_criterion = RowScopes.of(related_binding).get_criterion(
            join_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
            application_context=application_context,
        )
        if ambient_criterion is not None:
            joins[position] = (join_table, join_criterion & ambient_criterion)

    @staticmethod
    def fold_through_model_ambient_scope_into_join(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
        application_context: HareContext | None = None,
    ) -> None:
        """ANDs a through model's own default scope into ``joins[0]`` of a many-to-many hop, in place -
        a soft-deleted through row is no link. The selected client is checked for the declaring
        model and any explicit through model; an automatic through table has no model scope.
        A no-op for any other relation.
        """
        if not isinstance(related_field, ManyToManyFieldInstance):
            return
        through_binding = related_field.get_through_binding()
        if connection is not None:
            declaring_binding = (
                related_field.get_related_binding()
                if related_field._generated
                else related_field.get_binding().model_binding
            )
            if declaring_binding.checks_tenant_client:  # type: ignore[union-attr]
                declaring_binding.check_tenant_client(connection)  # type: ignore[union-attr]
        if through_binding is None:
            return
        ScopeJoinConditions.fold_ambient_scope_into_join(
            joins,
            through_binding,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
            application_context=application_context,
            position=0,
        )

    @staticmethod
    def fold_visible_target_into_through_join(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
        application_context: HareContext | None = None,
    ) -> None:
        """ANDs into ``joins[0]`` of a many-to-many hop an ``EXISTS`` that the linked row is visible in
        the related model's default scope - a through row pointing at a hidden row joins nothing.

        Args:
            joins: The through-table join followed by the related-table join; changed in place.
            related_field: The relation; anything but a many-to-many field is left unchanged.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
            application_context: The application context used to bind values and check model routing.
        """
        if not isinstance(related_field, ManyToManyFieldInstance):
            return
        # Local imports: hare.query.managers.manager and hare.query.expressions.subqueries.subquery both import this
        # module at module level.
        from hare.query.scopes.row_scopes import RowScopes

        through_table, through_criterion = joins[0]
        related_meta = related_field.get_related_binding()
        visible_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{through_table.get_table_name()}__visible")
        )
        visible_criterion = RowScopes.of(related_meta).get_criterion(
            visible_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
            application_context=application_context,
        )
        if visible_criterion is None:
            return
        if connection is not None and related_meta.checks_tenant_client:
            related_meta.check_tenant_client(connection)
        visible_exists = ScopeJoinConditions.get_through_target_exists(
            through_table, visible_table, related_field.forward_keys, related_meta, visible_criterion
        )
        joins[0] = (through_table, through_criterion & visible_exists)

    @staticmethod
    def fold_extra_condition_into_join(
        joins: list[TableCriterionTuple],
        related_binding: ModelBinding,
        extra_condition: Q | None,
        *,
        dialect: Dialect,
        connection: DatabaseClient | None,
        value_wrapper_references: RecordedValueReferences | None = None,
        application_context: HareContext | None = None,
    ) -> None:
        """ANDs a ``Select(relation, extra_condition=Q(...))`` declared for the relation into
        ``joins[-1]``, in place - the JOIN built first is the one kept.
        """
        if extra_condition is None:
            return
        # Local imports: ExpressionContext's own module and the plans package import this one.
        from hare.query.expressions.expression_context import ExpressionContext
        from hare.query.plans.recording.plan_recording import PlanRecording

        # Its values bound from the query holding it when a plan runs.
        records_join_condition = value_wrapper_references is None and PlanRecording.is_recording()
        if records_join_condition:
            value_wrapper_references = []
        join_table, join_criterion = joins[-1]
        extra_modifier = extra_condition.get_result(
            ExpressionContext(
                binding=related_binding,
                table=join_table,
                annotations={},
                dialect=dialect,
                connection=connection,
                value_wrapper_references=value_wrapper_references,
                application_context=application_context,
            )
        )
        if records_join_condition and value_wrapper_references is not None:
            PlanRecording.record_join_condition(value_wrapper_references)
        if extra_modifier.joins:
            raise QueryError(
                "Select(relation, extra_condition=...) only supports direct fields of the related model, "
                "not a further relation"
            )
        if extra_modifier.where_criterion:
            joins[-1] = (join_table, join_criterion & extra_modifier.where_criterion)

    @staticmethod
    def fold_extra_condition_into_through_join(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        extra_condition: Q | None,
        *,
        dialect: Dialect,
        connection: DatabaseClient | None,
        application_context: HareContext | None = None,
    ) -> None:
        """ANDs into ``joins[0]`` of a many-to-many hop an ``EXISTS`` that the linked row meets the hop's
        extra condition - a link to a row the condition leaves out joins nothing, instead of a row
        whose related values are NULL.

        Args:
            joins: The through-table join followed by the related-table join; changed in place.
            related_field: The relation; anything but a many-to-many field is left unchanged.
            extra_condition: The condition, None for none.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
            application_context: The application context used to bind values and check model routing.
        """
        if extra_condition is None or not isinstance(related_field, ManyToManyFieldInstance):
            return
        # Local imports: ExpressionContext's own module and the subquery module import this one.
        from hare.query.expressions.expression_context import ExpressionContext

        through_table, through_criterion = joins[0]
        related_meta = related_field.get_related_binding()
        matched_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{through_table.get_table_name()}__matched")
        )
        # Local import: the plans package imports this module.
        from hare.query.plans.recording.plan_recording import PlanRecording

        # Its values bound from the query holding it when a plan runs.
        value_wrapper_references: RecordedValueReferences | None = [] if PlanRecording.is_recording() else None
        matched_criterion = extra_condition.get_result(
            ExpressionContext(
                binding=related_meta,
                table=matched_table,
                annotations={},
                dialect=dialect,
                connection=connection,
                value_wrapper_references=value_wrapper_references,
                application_context=application_context,
            )
        ).where_criterion
        if value_wrapper_references is not None:
            PlanRecording.record_join_condition(value_wrapper_references)
        if not matched_criterion:
            return
        matched_exists = ScopeJoinConditions.get_through_target_exists(
            through_table, matched_table, related_field.forward_keys, related_meta, matched_criterion
        )
        joins[0] = (through_table, through_criterion & matched_exists)

    @staticmethod
    def fold_through_join_scopes(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility,
        dialect: Dialect,
        connection: DatabaseClient | None,
        application_context: HareContext | None = None,
    ) -> None:
        """Folds into the joins of one relation hop the default scope of a real through model
        (``ScopeJoinConditions.fold_through_model_ambient_scope_into_join()``) and the visibility of the rows its
        through rows point at (``ScopeJoinConditions.fold_visible_target_into_through_join()``).

        Args:
            joins: The joins of the hop, changed in place.
            related_field: The relation crossed.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
            application_context: The application context used to bind values and check model routing.
        """
        ScopeJoinConditions.fold_through_model_ambient_scope_into_join(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
            application_context=application_context,
        )
        ScopeJoinConditions.fold_visible_target_into_through_join(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
            application_context=application_context,
        )
