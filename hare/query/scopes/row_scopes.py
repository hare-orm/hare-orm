from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.model_cache import ModelCache
from hare.exceptions import ConfigurationError, QueryError
from hare.query.expressions import ExpressionContext, Q
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.join_condition_recording import JoinConditionRecording
from hare.query.scopes.manager_scope import ManagerScope
from hare.query.scopes.row_scope import RowScope
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.soft_delete_scope import SoftDeleteScope
from hare.query.scopes.tenant_scope import TenantScope

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
    from hare.query.queryset import QuerySet
    from hare.sql import Table
    from hare.sql.terms.criteria.criterion import Criterion


class RowScopes:
    """The default scopes of one model (``RowScopes.of(model)``): what its own queries are filtered by,
    and what a JOIN to it adds to its ON clause - a JOIN never goes through the model's manager.

    Args:
        model: The model.
    """

    #: The model and visibility a query made for another query is being built with - read by the
    #: base Manager.get_queryset(), so an override calling super() gets them. Applies to that
    #: model's manager only.
    requested_visibility: ClassVar[ContextVar[tuple[type[Model], RowVisibility] | None]] = ContextVar(
        "hare_requested_row_visibility", default=None
    )

    __slots__ = ("model", "field_scopes", "scopes")

    def __init__(self, model: type[Model]) -> None:
        # Deferred import: hare.query.manager imports this module.
        from hare.query.manager import Manager

        meta = model._meta
        self.model = model
        #: The scopes filtering by one field of the model, soft-delete first.
        self.field_scopes: tuple[RowScope, ...] = (
            *((SoftDeleteScope(model),) if meta.soft_delete_field else ()),
            *((TenantScope(model),) if meta.tenant_field else ()),
        )
        #: Every scope - a custom manager's last.
        self.scopes: tuple[RowScope, ...] = (
            *self.field_scopes,
            *((ManagerScope(model),) if Manager.has_custom_get_queryset(model) else ()),
        )

    def __bool__(self) -> bool:
        return bool(self.scopes)

    @staticmethod
    @ModelCache.fact()
    def of(model: type[Model]) -> RowScopes:
        """The scopes of ``model``.

        Args:
            model: The model.

        Returns:
            The scopes - falsy when the model has none.
        """
        return RowScopes(model)

    @staticmethod
    def get_base_queryset(model: type[Model], visibility: RowVisibility = RowVisibility.DEFAULT) -> QuerySet[Model]:
        """A queryset of ``model`` scoped only by ``Meta.soft_delete_field``/``Meta.tenant_field``,
        without the filters a custom ``Meta.manager`` adds.

        Args:
            model: The model.
            visibility: Which rows the queryset asks to see.

        Returns:
            The queryset, bound to no connection.
        """
        # Deferred import: the queryset package imports this module.
        from hare.query.queryset import QuerySet

        queryset = QuerySet(model)
        queryset._uses_default_scope = True
        queryset._visibility = visibility
        return queryset

    @staticmethod
    def get_queryset(model: type[Model], visibility: RowVisibility = RowVisibility.DEFAULT) -> QuerySet[Model]:
        """The queryset ``model``'s manager returns, seeing what a query with ``visibility``
        sees.

        Args:
            model: The model.
            visibility: The visibility of the query it is made for.

        Returns:
            The queryset.
        """
        token = RowScopes.requested_visibility.set((model, visibility.get_for_related_query()))
        try:
            return model._meta.manager.get_queryset()
        finally:
            RowScopes.requested_visibility.reset(token)

    def get_filters(self, visibility: RowVisibility, *, uses_default_scope: bool = True) -> list[tuple[str, Any]]:
        """The ``(field_name, value)`` filters a query of the model itself runs under - a custom
        manager's are in the queryset it returned already.

        Args:
            visibility: Which rows the query asks to see.
            uses_default_scope: Whether the query came from the model's manager - one that didn't
                is only filtered by what it asked for itself (``.only_deleted()``).

        Returns:
            The filters, soft-delete first.

        Raises:
            QueryError: If the model is tenant-scoped, the tenant filter isn't switched off and
                no tenant is active.
        """
        filters: list[tuple[str, Any]] = []
        if uses_default_scope:
            for scope in self.field_scopes:
                scope_filter = scope.get_filter(visibility)
                if scope_filter is not None:
                    filters.append(scope_filter)
        if visibility.only_deleted:
            filters.append((f"{self.model._meta.soft_delete_field}__isnull", False))
        return filters

    def get_filters_plan_description(
        self, visibility: RowVisibility, *, uses_default_scope: bool = True
    ) -> tuple[PlanDescription, tuple[str, ...]] | None:
        """Describes the filters a query of the model itself runs under (``get_filters()``) - each
        one a ``Q`` put in front of the query's own filters, its values bound before theirs.

        Args:
            visibility: Which rows the query asks to see.
            uses_default_scope: Whether the query came from the model's manager.

        Returns:
            The description and the filter key of each of its values; None when no tenant is
            active.
        """
        try:
            filters = self.get_filters(visibility, uses_default_scope=uses_default_scope)
        except QueryError:
            return None
        structures: list[Any] = []
        values: list[Any] = []
        keys: list[str] = []
        for key, value in filters:
            value_count = len(values)
            structures.append((key, Q.describe_value_filter(key, value, values)))
            keys += [key] * (len(values) - value_count)
        return PlanDescription(tuple(structures), values), tuple(keys)

    def get_condition(self, visibility: RowVisibility = RowVisibility.DEFAULT) -> Q | None:
        """The condition a JOIN to the model folds into its ON clause, so the joined rows are
        scoped exactly like the model's own queries.

        Args:
            visibility: The visibility of the query the JOIN is built for.

        Returns:
            Every scope's condition ``AND``ed together, or None if the model has none.

        Raises:
            QueryError: If the model is tenant-scoped, the tenant filter isn't switched off and
                no tenant is active.
            ConfigurationError: If the manager's queryset carries state with no condition
                equivalent.
        """
        condition: Q | None = None
        for scope in self.scopes:
            scope_condition = scope.get_condition(visibility)
            if scope_condition is not None:
                condition = scope_condition if condition is None else condition & scope_condition
        return condition

    def get_criterion(
        self,
        table: Table,
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> Criterion | None:
        """``get_condition()`` as a ``hare.sql`` criterion against ``table`` - for a JOIN or a WHERE
        built by hand.

        Args:
            table: The table of the model, possibly aliased.
            visibility: The visibility of the query it is built for.
            dialect: The dialect the criterion compiles for.
            connection: The connection the query runs on, None for no particular database.

        Returns:
            The criterion, None if the model has no default scope.

        Raises:
            QueryError: As ``get_condition()``.
            ConfigurationError: The manager's scope crosses a relation, or as ``get_condition()``.
        """
        if not self.scopes:
            return None
        condition = self.get_condition(visibility)
        model = self.model
        if condition is None:
            # A plan of the query has to tell a scope that has a condition now.
            JoinConditionRecording.record_scope(model, visibility, PlanDescription(None, []), [])
            return None
        # A query recording its plan records the scope's values apart from its own - a later
        # query of the plan binds the scope of its own context (JoinConditionRecording).
        value_wrapper_refs: RecordedValueRefs | None = [] if JoinConditionRecording.is_recording() else None
        modifier = condition.get_result(
            ExpressionContext(
                model=model,
                table=table,
                annotations={},
                dialect=dialect,
                connection=connection,
                value_wrapper_refs=value_wrapper_refs,
            )
        )
        if value_wrapper_refs is not None:
            JoinConditionRecording.record_scope(
                model, visibility, condition.get_plan_description(PlanContext.EMPTY), value_wrapper_refs
            )
        if modifier.joins:
            raise ConfigurationError(
                f"{model.__name__}'s Meta.manager get_queryset() filters across a relation, which a "
                f"JOIN to {model.__name__} can't carry in its ON condition - only direct fields of "
                f"{model.__name__} are supported in its default scope"
            )
        return modifier.where_criterion or None
