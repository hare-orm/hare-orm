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

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions import Q
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences


class ScopeJoinConditions:
    """The conditions folded into the ON clause of a relation's JOIN: the related model's default and
    tenant scope, the through model's scope and the visible target of a many-to-many link, and the
    extra condition of Select(relation, extra_condition=...)."""

    @staticmethod
    def fold_ambient_scope_into_join(
        joins: list[TableCriterionTuple],
        related_model: type[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """ANDs the related model's default scope into ``joins[-1]``, in place - a JOIN never goes
        through the model's manager.

        Args:
            joins: The joins; the last one targets ``related_model``.
            related_model: The related model.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
        """
        # Local import: hare.query.managers.manager imports QuerySet, which eventually imports this module
        # at module level - importing it back here at module level would be circular.
        from hare.query.scopes.row_scopes import RowScopes

        join_table, join_criterion = joins[-1]
        ambient_criterion = RowScopes.of(related_model).get_criterion(
            join_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if ambient_criterion is not None:
            joins[-1] = (join_table, join_criterion & ambient_criterion)

    @staticmethod
    def fold_through_model_ambient_scope_into_join(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """ANDs a through model's own default scope into ``joins[0]`` of a many-to-many hop, in place -
        a soft-deleted through row is no link. A no-op for any other relation and for a through
        table without a model.
        """
        if not isinstance(related_field, ManyToManyFieldInstance):
            return
        through_model = related_field.through_model_class
        if through_model is None:
            return
        # Local import: same circularity reasoning as ScopeJoinConditions.fold_ambient_scope_into_join() above.
        from hare.query.scopes.row_scopes import RowScopes

        join_table, join_criterion = joins[0]
        ambient_criterion = RowScopes.of(through_model).get_criterion(
            join_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if ambient_criterion is not None:
            joins[0] = (join_table, join_criterion & ambient_criterion)

    @staticmethod
    def fold_visible_target_into_through_join(
        joins: list[TableCriterionTuple],
        related_field: RelationalField[Model],
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> None:
        """ANDs into ``joins[0]`` of a many-to-many hop an ``EXISTS`` that the linked row is visible in
        the related model's default scope - a through row pointing at a hidden row joins nothing.

        Args:
            joins: The through-table join followed by the related-table join; changed in place.
            related_field: The relation; anything but a many-to-many field is left unchanged.
            visibility: Which rows the default scopes let the query see.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on, None when compiling for none.
        """
        if not isinstance(related_field, ManyToManyFieldInstance):
            return
        # Local imports: hare.query.managers.manager and hare.query.expressions.subqueries.subquery both import this
        # module at module level.
        from hare.query.scopes.row_scopes import RowScopes
        from hare.sql.terms.subqueries.exists_term import ExistsTerm

        through_table, through_criterion = joins[0]
        related_meta = related_field.related_model._meta
        visible_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{through_table.get_table_name()}__visible")
        )
        visible_criterion = RowScopes.of(related_field.related_model).get_criterion(
            visible_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if visible_criterion is None:
            return
        link_criterion = KeyColumns.row_equality(
            [through_table[column] for column in related_field.forward_keys],
            [visible_table[column] for column in KeyColumns.get_source_columns(related_meta)],
        )
        visible_exists = ExistsTerm(
            QueryBuilder().from_(visible_table).select(Star()).where(link_criterion & visible_criterion)
        )
        joins[0] = (through_table, through_criterion & visible_exists)

    @staticmethod
    def fold_extra_condition_into_join(
        joins: list[TableCriterionTuple],
        related_model: type[Model],
        extra_condition: Q | None,
        *,
        dialect: Dialect,
        connection: DatabaseClient | None,
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
        value_wrapper_references: RecordedValueReferences | None = [] if PlanRecording.is_recording() else None
        join_table, join_criterion = joins[-1]
        extra_modifier = extra_condition.get_result(
            ExpressionContext(
                model=related_model,
                table=join_table,
                annotations={},
                dialect=dialect,
                connection=connection,
                value_wrapper_references=value_wrapper_references,
            )
        )
        if value_wrapper_references is not None:
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
        """
        if extra_condition is None or not isinstance(related_field, ManyToManyFieldInstance):
            return
        # Local imports: ExpressionContext's own module and the subquery module import this one.
        from hare.query.expressions.expression_context import ExpressionContext
        from hare.sql.terms.subqueries.exists_term import ExistsTerm

        through_table, through_criterion = joins[0]
        related_meta = related_field.related_model._meta
        matched_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{through_table.get_table_name()}__matched")
        )
        # Local import: the plans package imports this module.
        from hare.query.plans.recording.plan_recording import PlanRecording

        # Its values bound from the query holding it when a plan runs.
        value_wrapper_references: RecordedValueReferences | None = [] if PlanRecording.is_recording() else None
        matched_criterion = extra_condition.get_result(
            ExpressionContext(
                model=related_field.related_model,
                table=matched_table,
                annotations={},
                dialect=dialect,
                connection=connection,
                value_wrapper_references=value_wrapper_references,
            )
        ).where_criterion
        if value_wrapper_references is not None:
            PlanRecording.record_join_condition(value_wrapper_references)
        if not matched_criterion:
            return
        link_criterion = KeyColumns.row_equality(
            [through_table[column] for column in related_field.forward_keys],
            [matched_table[column] for column in KeyColumns.get_source_columns(related_meta)],
        )
        matched_exists = ExistsTerm(
            QueryBuilder().from_(matched_table).select(Star()).where(link_criterion & matched_criterion)
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
        """
        ScopeJoinConditions.fold_through_model_ambient_scope_into_join(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        ScopeJoinConditions.fold_visible_target_into_through_join(
            joins,
            related_field,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
