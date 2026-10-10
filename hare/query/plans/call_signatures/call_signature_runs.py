from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from hare.core.caching.model_cache import ModelCache
from hare.dialects.base.client.database_client import DatabaseClient, retryable_read_query_active
from hare.fields.enums import RelationLoadStrategy
from hare.query.expressions import Subquery
from hare.query.expressions.constants import CALL_SIGNATURE_DESCRIBED_KEY_SUFFIXES, PLAIN_VALUE_TYPES
from hare.query.plans.call_signatures.call_signature_plans import CallSignaturePlans
from hare.query.plans.constants import CALL_SIGNATURE_DESCRIBED_VALUE_TYPES, SELECT_FOR_UPDATE_OPTION_NAMES
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.arguments.filter_arguments import FilterArguments
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.rows.model_rows.model_rows import ModelRows
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.values.values_reading import ValuesReading
from hare.query.statements.select.values_query import ValuesQuery
from hare.sql.terms.term import Term
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.queryset import QuerySet


class CallSignatureRuns:
    """The runs of a queryset of model instances made by simple calls alone on the plan kept under
    the key of its calls (``CallSignaturePlans``) - no query is made, the queryset's values are bound
    into the plan's statement and its rows read."""

    @staticmethod
    @ModelCache.fact()
    def model_takes_plan_runs(model: type[Model]) -> bool:
        """Whether ``model``'s queries have nothing added by default but the filters of
        ``Meta.tenant_field``/``Meta.soft_delete_field`` - no custom ``get_queryset()`` and no
        relation loaded by default (``lazy="joined"``/``"select"``).

        Args:
            model: The model.

        Returns:
            True when its querysets may run on a plan without making a query.
        """
        row_scopes = RowScopes.of(model)
        if len(row_scopes.scopes) > len(row_scopes.field_scopes):
            return False
        meta = model._meta
        for field_name in meta.foreign_key_fields | meta.one_to_one_fields | meta.many_to_many_fields:
            if getattr(meta.fields_map[field_name], "lazy", None) in {
                RelationLoadStrategy.JOINED,
                RelationLoadStrategy.SELECT,
            }:
                return False
        return True

    @staticmethod
    def get_get_call(queryset: QuerySet[Any, Any], kwargs: dict[str, Any]) -> tuple[Any, ...] | None:
        """The call ``get(**kwargs)`` of a queryset nothing else changed is in the call signature it
        starts - its filter keys, the types of their values, the row lock and the rows seen, which
        the queryset's other calls don't show. Its values are plain ones, each bound as given.

        Args:
            queryset: The queryset.
            kwargs: The filters.

        Returns:
            The call; None when the queryset was changed otherwise, a filter is built and checked
            right away, or a value is no plain one (a list, a None, a boolean, a query or an
            expression) - ``get()`` then filters it as usual.
        """
        call_signature = queryset._call_signature
        if call_signature is not None and len(call_signature) == 1:
            # The manager's unchanged queryset (or its all()): every other call clones the queryset
            # with no signature or extends it - no option, condition or row visibility is set.
            if not FilterArguments.takes_pending_filters(queryset, kwargs):
                return None
            for key, value in kwargs.items():
                if (
                    type(value) not in PLAIN_VALUE_TYPES
                    and (
                        type(value) in CALL_SIGNATURE_DESCRIBED_VALUE_TYPES
                        or isinstance(value, (Plannable, Term, QuerySpecification, Subquery))
                    )
                ) or key.endswith(CALL_SIGNATURE_DESCRIBED_KEY_SUFFIXES):
                    return None
            return ("get", tuple(kwargs), tuple([type(value) for value in kwargs.values()]), None, None)
        options = queryset._options
        lock_structure = None
        if options is not QueryOptions.DEFAULT:
            # A queryset whose only option is select_for_update() runs on the plan of its lock.
            if not options.without(*SELECT_FOR_UPDATE_OPTION_NAMES).is_default():
                return None
            lock_structure = tuple([getattr(options, name) for name in SELECT_FOR_UPDATE_OPTION_NAMES])
        if not (
            not queryset._q_object_list
            and not queryset._pending_filter_calls
            and not queryset._annotations
            and not queryset._select_related
            and not queryset._prefetch_map
            and not queryset._prefetch_queries
            and not queryset._orderings
            and queryset._limit is None
            and not queryset._offset
            and not queryset._distinct
            and not queryset._is_none
            and queryset._selection is None
            and queryset._combination is None
            and queryset.model._meta._inited
            and FilterArguments.takes_pending_filters(queryset, kwargs)
        ):
            return None
        for key, value in kwargs.items():
            if (
                type(value) in CALL_SIGNATURE_DESCRIBED_VALUE_TYPES
                or isinstance(value, (Plannable, Term, QuerySpecification, Subquery))
                or key.endswith(CALL_SIGNATURE_DESCRIBED_KEY_SUFFIXES)
            ):
                return None
        visibility = queryset._visibility
        # The default scope's structure is in the key; the rows seen through relations are not.
        visibility_structure = (
            None
            if visibility is RowVisibility.DEFAULT
            else (visibility.all_tenants, visibility.include_deleted, visibility.only_deleted)
        )
        value_types = tuple([type(value) for value in kwargs.values()])
        return ("get", tuple(kwargs), value_types, lock_structure, visibility_structure)

    @staticmethod
    def leaves_row_lock_to_query(connection: DatabaseClient) -> bool:
        """Whether a queryset's row lock on ``connection`` is left to the query run as usual - refused
        there outside a transaction, or taken by the keys of its rows where the database locks rows
        that way. Asked only of a queryset taking a row lock.

        Args:
            connection: The connection the queryset runs on.

        Returns:
            Whether the queryset doesn't run on a plan.
        """
        return not connection.is_transaction_client or connection.features.locks_rows_by_key

    @staticmethod
    def await_call_signature_rows(
        queryset: QuerySet[Any, Any], connection: DatabaseClient
    ) -> Generator[Any, None, Any] | None:
        """Runs a queryset of model instances, made by simple calls alone, on the plan kept under the
        key of its calls - no query is made.

        Args:
            queryset: The queryset.
            connection: The connection it runs on.

        Returns:
            The awaitable's generator; None when there is no plan to run on yet, the model's rows
            take more than the plan (relations loaded by default, a custom manager), or a row lock
            is taken outside a transaction - the queryset is then run as usual.
        """
        model = queryset.model
        if not CallSignatureRuns.model_takes_plan_runs(model) or not connection.features.supports_positional_rows:
            return None
        if (
            queryset._options is not QueryOptions.DEFAULT
            and queryset._select_for_update
            and CallSignatureRuns.leaves_row_lock_to_query(connection)
        ):
            return None
        signature_key = CallSignaturePlans.get_key(ModelRowsQuery, queryset, connection, ())
        if signature_key is None:
            return None
        key, values, visibility = signature_key
        found = CallSignaturePlans.find_with_value_positions(model, key)
        if found is None:
            return None
        plan = found[0]
        if plan.sql is None or plan.decode_plan is None:
            return None
        positions = found[1]
        if positions is not None:
            # The plan binds the call values in another order.
            values = [values[position] for position in positions]
        if plan.scope_sources:
            scope_values = plan.get_scope_values()
            if scope_values is None:
                return None
            values = [*values, *scope_values]
        parameters = plan.bind(values, model, connection.dialect, queryset._limit, queryset._offset)
        if parameters is None:
            return None
        StatementPlans.count_hit()
        if queryset._prefetch_map or queryset._prefetch_queries:
            return CallSignatureRuns.fetch_and_prefetch_on_plan(
                queryset, plan, parameters, connection, visibility
            ).__await__()
        return CallSignatureRuns.fetch_on_plan(queryset, plan, parameters, connection).__await__()

    @staticmethod
    def await_call_signature_values(
        queryset: QuerySet[Any, Any], connection: DatabaseClient
    ) -> Generator[Any, None, Any] | None:
        """Runs a ``.values()``/``.values_list()`` queryset, made by simple calls alone, on the plan kept
        under the key of its calls - no query is made; the rows are read as the plan keeps it.

        Args:
            queryset: The queryset.
            connection: The connection it runs on.

        Returns:
            The awaitable's generator; None when there is no plan to run on yet, the model's queries
            take more than the plan, or a row lock is taken outside a transaction - the queryset is
            then run as usual.
        """
        model = queryset.model
        if not CallSignatureRuns.model_takes_plan_runs(model):
            return None
        if (
            queryset._options is not QueryOptions.DEFAULT
            and queryset._select_for_update
            and CallSignatureRuns.leaves_row_lock_to_query(connection)
        ):
            return None
        shape = queryset._selection.shape  # type: ignore[union-attr]
        # Keyed as ValuesQuery._get_call_signature_type_part() keys it - by the shape of the rows.
        signature_key = CallSignaturePlans.get_key(ValuesQuery, queryset, connection, (shape,))
        if signature_key is None:
            return None
        key, values, _visibility = signature_key
        found = CallSignaturePlans.find_with_value_positions(model, key)
        if found is None:
            return None
        plan = found[0]
        if plan.sql is None:
            return None
        values_reading = plan.result_reading.get(shape) if isinstance(plan.result_reading, dict) else None
        if values_reading is None:
            return None
        positions = found[1]
        if positions is not None:
            # The plan binds the call values in another order.
            values = [values[position] for position in positions]
        if plan.scope_sources:
            scope_values = plan.get_scope_values()
            if scope_values is None:
                return None
            values = [*values, *scope_values]
        parameters = plan.bind(values, model, connection.dialect, queryset._limit, queryset._offset)
        if parameters is None:
            return None
        StatementPlans.count_hit()
        return CallSignatureRuns.fetch_values_on_plan(
            queryset, plan, values_reading, parameters, connection
        ).__await__()

    @staticmethod
    async def fetch_values_on_plan(
        queryset: QuerySet[Any, Any],
        plan: StatementPlan,
        values_reading: ValuesReading,
        parameters: list[Any],
        connection: DatabaseClient,
    ) -> Any:
        """Runs a plan's statement with a values queryset's values bound and reads its rows.

        Args:
            queryset: The queryset.
            plan: The plan.
            values_reading: How the plan reads rows of the queryset's shape.
            parameters: The parameters (``StatementPlan.bind()``).
            connection: The connection.

        Returns:
            The rows; for a single-row queryset the row, or None for a ``first()`` that matched none.
        """
        # A read - retried on a lost connection, like every queryset read.
        token = retryable_read_query_active.set(True) if connection.read_retry_max_retries else None
        try:
            rows = await values_reading.rows.fetch(
                connection,
                plan.sql,  # type: ignore[arg-type]
                parameters,
                values_reading.column_converters,
                queryset.model,
                values_reading.value_fields,
            )
        finally:
            if token is not None:
                retryable_read_query_active.reset(token)
        if not queryset._single:
            return rows
        if len(rows) == 1:
            return rows[0]
        return CallSignatureRuns.get_missing_single_row_result(queryset, rows)

    @staticmethod
    def get_missing_single_row_result(queryset: QuerySet[Any, Any], rows: list[Any]) -> None:
        """What a single-row queryset run on a plan returns when it read no row or more than one.

        Args:
            queryset: The queryset.
            rows: The rows read - none, or more than one.

        Returns:
            None for a ``first()``-like queryset that matched no row.

        Raises:
            DoesNotExist: A ``.get()`` matched no row.
            MultipleObjectsReturned: More than one row matched.
        """
        if rows:
            GetExceptions.raise_multiple_objects_returned(queryset)
        if queryset._raise_does_not_exist:
            GetExceptions.raise_object_does_not_exist(queryset)
        return

    @staticmethod
    async def fetch_on_plan(
        queryset: QuerySet[Any, Any], plan: StatementPlan, parameters: list[Any], connection: DatabaseClient
    ) -> Any:
        """Runs a plan's statement with the queryset's values bound and reads its rows.

        Args:
            queryset: The queryset.
            plan: The plan.
            parameters: The parameters (``StatementPlan.bind()``).
            connection: The connection.

        Returns:
            The instances; for a single-row queryset the instance, or None for a ``first()``/
            ``get(does_not_exist_exception=None)`` that matched no row.
        """
        model = queryset.model
        zone_name = Timezone.get_aware_zone_name()
        reader = plan.model_readers.get(zone_name)
        if reader is None and plan.decode_plan is not None and HydrateAccelerator.module is not None:
            # The model's columns alone, in its own order - read by the native reader at once.
            reader = plan.model_readers[zone_name] = HydrateAccelerator.get_model_reader(
                model, plan.decode_plan, plan.decode_plan_is_partial, connection.dialect.types, zone_name
            )
        sql: str = plan.sql  # type: ignore[assignment]  # a plan run here always has its SQL
        # A read - retried on a lost connection, like every queryset read.
        token = retryable_read_query_active.set(True) if connection.read_retry_max_retries else None
        try:
            if reader is None:
                instances = await CallSignatureRuns.get_plan_model_rows(model, plan, connection).fetch(sql, parameters)
            else:
                # Run on a plan only where the rows are read by position (model_takes_plan_runs()).
                result = await connection.execute(sql, parameters, returns_rows=True, rows_by_position=True)
                instances = []
                if result.rows:
                    try:
                        instances = reader.read(result.rows, connection.connection_alias, 0, False)
                    except (TypeError, AttributeError):
                        # A stale build, possibly - the pure-Python read settles it.
                        instances = await CallSignatureRuns.get_plan_model_rows(model, plan, connection).read_all(
                            result
                        )
        finally:
            if token is not None:
                retryable_read_query_active.reset(token)
        if not queryset._single:
            return instances
        if len(instances) == 1:
            return instances[0]
        return CallSignatureRuns.get_missing_single_row_result(queryset, instances)

    @staticmethod
    async def fetch_and_prefetch_on_plan(
        queryset: QuerySet[Any, Any],
        plan: StatementPlan,
        parameters: list[Any],
        connection: DatabaseClient,
        visibility: RowVisibility | None,
    ) -> Any:
        """``fetch_on_plan()`` of a queryset prefetching relations - prefetched for the instances read,
        with the settings the built query hands its prefetch queries.

        Args:
            queryset: The queryset.
            plan: The plan.
            parameters: The parameters (``StatementPlan.bind()``).
            connection: The connection.
            visibility: The visibility the default scope was resolved with (None for a model without
                one).

        Returns:
            What ``fetch_on_plan()`` returns.
        """
        # Deferred import: the prefetch module builds querysets, which import this module.
        from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
        from hare.query.relation_loading.prefetching.prefetcher import Prefetcher

        single = queryset._single
        queryset._single = False
        try:
            instances = await CallSignatureRuns.fetch_on_plan(queryset, plan, parameters, connection)
        finally:
            queryset._single = single
        if instances:
            await Prefetcher(queryset.model, connection).prefetch(
                instances,
                PrefetchRequest(
                    queryset._prefetch_map,
                    queryset._prefetch_queries,
                    visibility=visibility if visibility is not None else queryset._visibility.get_for_active_tenant(),
                    select_for_update=queryset._select_for_update,
                    select_for_update_nowait=queryset._select_for_update_nowait,
                    select_for_update_skip_locked=queryset._select_for_update_skip_locked,
                    select_for_update_strength=queryset._select_for_update_strength,
                    db_explicitly_chosen=queryset._connection_explicitly_chosen,
                ),
            )
        if not single:
            return instances
        if len(instances) == 1:
            return instances[0]
        return CallSignatureRuns.get_missing_single_row_result(queryset, instances)

    @staticmethod
    def get_plan_model_rows(model: type[Model], plan: StatementPlan, connection: DatabaseClient) -> ModelRows:
        """The reader of a plan's model rows - ``fetch_on_plan()`` without the native reader.

        Args:
            model: The model queried.
            plan: The plan.
            connection: The connection.

        Returns:
            The reader.
        """
        return ModelRows(
            model,
            connection,
            select_related_buckets=list(plan.select_related_positions),
            decode_plan=plan.decode_plan,
            decode_plan_is_partial=plan.decode_plan_is_partial,
        )
