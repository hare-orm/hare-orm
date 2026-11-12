from __future__ import annotations

from collections.abc import Coroutine, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import QueryError
from hare.query.constants import PLAN_CACHE_MISS
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.call_signatures.call_signature_plans import CallSignaturePlans
from hare.query.plans.constants import EXPRESSIONS_NESTED_TOO_DEEPLY_MESSAGE, NO_CALL_PLAN, OPTIONAL_VALUE_ORIGIN
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement.declarations import CallPlanHit, CallPlanLookup, RenderedStatement
from hare.query.plans.statement.query_key_compiler import QueryKeyCompiler
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.scopes.row_scopes import RowScopes
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.time.zone_settings import ZoneSettings

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.expressions.value_references.value_reference_types import ValueReference
    from hare.query.plans.recording.plan_recording import PlanRecording
    from hare.query.plans.recording.scope_value_source import ScopeValueSource
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.queryset.queryset import QuerySet
    from hare.query.queryset.selection.statement_selection import StatementSelection
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.query.statements.select.model_rows_query import ModelRowsQuery
    from hare.query.statements.select.values_query import ValuesQuery


class StatementPlanRuns:
    """How a query runs on a kept plan: the plan found under the key of the calls its queryset was made
    with before any query object is made, or under its description's key while it is built; the
    query's values bound into the plan's statement; and the plan of a query just built kept for the
    later queries of its key."""

    #: The classes ``read_queryset()`` makes queries with - imported at its first call, as their modules
    #: import this one.
    statement_selection_class: ClassVar[DeferredImport[type[StatementSelection]]] = DeferredImport(
        "hare.query.queryset.selection.statement_selection", "StatementSelection"
    )
    model_rows_query_class: ClassVar[DeferredImport[type[ModelRowsQuery[Any]]]] = DeferredImport(
        "hare.query.statements.select.model_rows_query", "ModelRowsQuery"
    )
    values_query_class: ClassVar[DeferredImport[type[ValuesQuery]]] = DeferredImport(
        "hare.query.statements.select.values_query", "ValuesQuery"
    )

    @staticmethod
    def find_call_plan(
        query_class: type[AwaitableQuery[Any]],
        specification: QuerySpecification[Any],
        connection: DatabaseClient,
        type_part: tuple[Any, Sequence[Any]] = ((), ()),
    ) -> CallPlanHit | CallPlanLookup:
        """The plan a query of ``query_class`` of a queryset made by simple calls alone runs on - found
        by the key of the calls, before any query object is made or copied. ``specification`` is
        only read.

        Args:
            query_class: The class of the query run.
            specification: The queryset, or the query made from it, with the calls.
            connection: This execution's connection.
            type_part: What the key and the values hold of ``query_class`` beyond the calls
                (``_get_call_signature_type_part()``) - a None structure finds no plan.

        Returns:
            The hit; without one - the queryset wasn't made by simple calls alone, no plan is kept
            under the key yet, or this execution's values don't fit the plan - the caller builds the
            query, keeping the plan it builds under ``record_key`` too.
        """
        if specification._call_signature is None or specification._extension_calls:  # type: ignore[attr-defined]
            return NO_CALL_PLAN
        type_structure, type_values = type_part
        if type_structure is None:
            return NO_CALL_PLAN
        if specification._row_scope_provider is not None:
            return NO_CALL_PLAN
        model = specification.model
        dialect = connection.dialect
        given_visibility = specification._visibility
        row_scopes = RowScopes.of(model)
        if row_scopes.field_scopes:
            visibility = given_visibility.get_for_active_tenant()
            scope = row_scopes.get_filters_plan_description(
                visibility, uses_default_scope=specification._uses_default_scope
            )
            if scope is None:
                return NO_CALL_PLAN
            scope_description = scope[0]
        else:
            visibility = None
            scope_description = PlanDescription.EMPTY
        call_signature = specification._call_signature  # type: ignore[attr-defined]
        call_values = specification._call_values  # type: ignore[attr-defined]
        last_call = call_signature[-1]
        if last_call is not call_signature[0] and last_call[0] == "get":
            # get() alone - it starts its signature - of plain values, each bound as given: the call
            # holds their types, the signature is the structure.
            calls_structure = call_signature
        else:
            try:
                calls_description = CallSignaturePlans.describe_calls(
                    call_signature,
                    call_values,
                    dialect.parameters.single_parameter_in_list_min_length,
                    model,
                )
            except RecursionError:
                # Described level by level, as the query is built - an expression nested hundreds of
                # levels deep is deeper than Python's stack before the build is reached.
                raise QueryError(EXPRESSIONS_NESTED_TOO_DEEPLY_MESSAGE) from None
            if calls_description is None:
                return NO_CALL_PLAN
            calls_structure, call_values = calls_description
        key = (
            "calls",
            query_class,
            dialect,
            QueryKeyCompiler.get_connection_structure(connection),
            ZoneSettings.get_rendered_zone_key(),
            scope_description.structure,
            # Which rows the default scopes of the models the query JOINs let it see.
            given_visibility.all_tenants,
            given_visibility.include_deleted,
            given_visibility.only_deleted,
            calls_structure,
            type_structure,
        )
        found = CallSignaturePlans.find_with_value_positions(model, key)
        if found is None:
            return CallPlanLookup(key, len(type_values))
        plan, positions, reading = found
        bound_values: Sequence[Any] = (
            [*scope_description.values, *call_values, *type_values]
            if scope_description.values or type_values
            else call_values
        )
        if positions is not None:
            # The plan binds the values in another order.
            bound_values = [bound_values[position] for position in positions]
        paginate = query_class.plan_binds_slice
        parameters = plan.bind(
            bound_values,
            model,
            dialect,
            specification._limit if paginate else PLAN_CACHE_MISS,
            specification._offset if paginate else PLAN_CACHE_MISS,
        )
        if parameters is None or plan.sql is None:
            # Kept already - this execution's values don't fit it.
            return NO_CALL_PLAN
        StatementPlans.count_hit()
        return CallPlanHit(
            plan,
            plan.sql,
            parameters,
            reading,
            visibility if visibility is not None else specification._visibility.get_for_active_tenant(),
        )

    @staticmethod
    def prepare_call_plan_record(query: AwaitableQuery[Any], lookup: CallPlanLookup) -> None:
        """Marks a query of a queryset's calls, made because no plan was found under their key, to keep
        the plan its build finds or records under that key too - when the origin of each value of the
        calls is known. Called on the query made to run, before anything changes it.

        Args:
            query: The query, holding the calls of its queryset (``QuerySet._hand_call_signature()``).
            lookup: The lookup that found no plan.
        """
        query._call_signature_record = None
        if lookup.record_key is None:
            return
        value_origins = CallSignaturePlans.get_value_origins(query, query._connection)
        if value_origins is not None:
            query._call_signature_record = (lookup.record_key, value_origins, lookup.type_value_count)

    @staticmethod
    def read_queryset(
        queryset: QuerySet[Any, Any], *, model_query_class: type[ModelRowsQuery[Any]] | None = None
    ) -> Coroutine[Any, Any, Any]:
        """Read rows inside their execution context, using the chosen model executor on hits and misses.

        The executor class is part of the call-plan key, so specialized model readers cannot
        share ordinary model-row plans' readers. Values and combinations retain their executors.
        """
        connection = None
        lookup = NO_CALL_PLAN
        if queryset._call_signature is not None and queryset._combination is None:
            if queryset._is_none:
                return StatementPlanRuns.read_empty(queryset)
            selection = queryset._selection
            query_class = (
                (model_query_class if model_query_class is not None else StatementPlanRuns.model_rows_query_class)
                if selection is None
                else StatementPlanRuns.values_query_class
            )
            connection = queryset.get_connection(for_write=queryset._select_for_update)
            call_plan = StatementPlanRuns.find_call_plan(
                query_class,
                queryset,
                connection,
                ((), ()) if selection is None else query_class.get_queryset_type_part(queryset),  # type: ignore[union-attr]
            )
            if isinstance(call_plan, CallPlanHit):
                if queryset._select_for_update and WrittenConnections.is_recording:
                    WrittenConnections.record(connection.connection_alias)
                return query_class.read_call_plan(queryset, connection, call_plan)
            lookup = call_plan
        query = StatementPlanRuns.statement_selection_class.get_select_query(
            queryset, model_query_class=model_query_class
        )
        query._is_execution_query = True
        if connection is not None:
            queryset._hand_call_signature(query)
            query._apply_connection(connection)
            StatementPlanRuns.prepare_call_plan_record(query, lookup)
        return query._execute_bound()

    @staticmethod
    async def read_empty(queryset: QuerySet[Any, Any]) -> Any:
        """Keep single-result errors at await time for an empty declaration."""
        return GetExceptions.empty_single_or_list_result(queryset)

    @staticmethod
    def find_plan(
        query: AwaitableQuery[Any],
        plan_key: tuple[Any, ...],
        current_values: list[Any],
        *,
        paginate: bool = False,
        plan: StatementPlan | None = None,
    ) -> CallPlanHit | None:
        """Bind the description's plan without retaining parameters on the query.

        Args:
            query: The query.
            plan_key: The plan key.
            current_values: This query's values, in the order its description lists them.
            paginate: Whether this query's LIMIT/OFFSET replace the plan's.
            plan: The plan when the caller already found it.

        Returns:
            The bound plan, or None when a full build is needed.
        """
        if plan is None:
            plan = StatementPlans.find(query.model, plan_key)
        if plan is None or plan.sql is None:
            return None
        parameters = plan.bind(
            current_values,
            query.model,
            query._connection.dialect,
            query._limit if paginate else PLAN_CACHE_MISS,
            query._offset if paginate else PLAN_CACHE_MISS,
        )
        if parameters is None:
            return None
        if query._call_signature_record is not None:
            # Kept under the key of the queryset's calls too, as a plan built now is.
            StatementPlanRuns.record_call_signature_plan(query, plan, query._get_plan_value_description())
        StatementPlans.count_hit()
        return CallPlanHit(plan, plan.sql, parameters, None, query._visibility)

    @staticmethod
    def record_plan(
        query: AwaitableQuery[Any],
        plan_key: tuple[Any, ...] | None,
        value_wrapper_references: RecordedValueReferences | None,
        description: PlanDescription | None,
        decode_plan: Any = None,
        decode_plan_is_partial: bool = False,
        select_related_positions: tuple[Any, ...] = (),
        decode_plan_key: tuple[str | None, ...] | None = None,
        binds_ctes: bool = False,
        result_reading: Any = None,
        extension_calls_applied: bool = False,
        plan_recording: PlanRecording | None = None,
        result_column_names: tuple[str, ...] = (),
    ) -> RenderedStatement | None:
        """Keeps the query just built as the plan of its key, when every value it holds was
        recorded with a reference a later query's value can replace - each reference under the
        origin of its value (``PlanOrigins``), as the query's description lists them.

        Args:
            query: The query.
            plan_key: The plan key, None for a query that keeps no plan.
            value_wrapper_references: The references recorded while building, None when not recorded.
            description: The values the plan binds, each with its origin.
            decode_plan: The row decode plan kept with the plan.
            decode_plan_is_partial: Whether the decode plan covers only some fields.
            select_related_positions: The select_related() row layout kept with the plan.
            decode_plan_key: The names of the base model's selected columns, kept with the plan.
            binds_ctes: Whether the references of the query's CTEs are among the recorded ones.
            result_reading: What else reading the result of the plan needs (StatementPlan).
            extension_calls_applied: Whether the calls of the dialect's QuerySet methods are applied
                to the query already - a query with calls is recorded only then (_make_query()),
                and never when an argument of a call can't be part of a key.
            plan_recording: What the build recorded of the conditions it folded into its JOINs.
        """
        if (
            plan_key is None
            or value_wrapper_references is None
            or (plan_recording is not None and not plan_recording.keeps_plan)
        ):
            return None
        if query._extension_calls and not extension_calls_applied:
            if StatementPlanDescriptions.get_extension_calls_structure(query) is not None:
                query._deferred_plan_record = {
                    "plan_key": plan_key,
                    "value_wrapper_references": value_wrapper_references,
                    "description": description,
                    "decode_plan": decode_plan,
                    "decode_plan_is_partial": decode_plan_is_partial,
                    "select_related_positions": select_related_positions,
                    "decode_plan_key": decode_plan_key,
                    "binds_ctes": binds_ctes,
                    "result_reading": result_reading,
                    "result_column_names": result_column_names,
                    "plan_recording": plan_recording,
                }
            return None
        if query.binds_expression_term_values:
            value_wrapper_references = [
                *value_wrapper_references,
                *QueryAnnotations.get_expression_term_value_references(query),
            ]
        scope_sources: list[ScopeValueSource] = []
        scope_value_sources: list[tuple[ValueReference, ...]] = []
        if plan_recording is not None:
            value_wrapper_references = [*value_wrapper_references, *plan_recording.join_condition_references]
            for scope_source, references_by_position in plan_recording.scope_references.items():
                if any(not references for references in references_by_position):
                    return None
                scope_sources.append(scope_source)
                scope_value_sources += [tuple(references) for references in references_by_position]
        value_sources = StatementPlanRuns.get_value_sources(description, value_wrapper_references)
        if value_sources is None:
            return None
        description_value_sources, repeated_values = value_sources
        # A query with a plan that didn't run on it - built to be shown or built into another
        # query, or with a value the plan can't bind - keeps that plan.
        existing_plan = StatementPlans.find(query.model, plan_key)
        if existing_plan is not None:
            query._statement_plan = existing_plan
            StatementPlanRuns.record_call_signature_plan(query, existing_plan, description)
            return None
        rendered, plan = StatementPlan.prepare(
            query.query,
            (*description_value_sources, *scope_value_sources),
            repeated_values,
            decode_plan,
            decode_plan_is_partial,
            select_related_positions,
            tuple(query._annotation_output_fields.items()),
            decode_plan_key,
            binds_ctes,
            result_reading,
            tuple(scope_sources),
            sql_context=query._connection.sql_context,
            result_column_names=result_column_names,
        )
        query._statement_plan = plan
        if plan is None:
            return rendered
        StatementPlans.record(query.model, plan_key, plan)
        StatementPlanRuns.record_call_signature_plan(query, plan, description)
        return rendered

    @staticmethod
    def get_value_sources(
        description: PlanDescription | None, value_wrapper_references: RecordedValueReferences
    ) -> tuple[tuple[tuple[ValueReference, ...], ...], tuple[tuple[int, int], ...]] | None:
        """The references of each value of a query's description - the ones the build recorded with
        its origin.

        Args:
            description: The description, with the origin of each value.
            value_wrapper_references: The references the build recorded, each with its value's origin.

        Returns:
            Per value its references, and the values listed again under an origin listed before,
            each with the position of the first.
            None when a reference can't be bound or comes from no value of the description, or a
            value has no reference.
        """
        if description is None or description.origins is None:
            return None
        origins = list(description.origins)
        first_position_by_origin: dict[Any, int] = {}
        repeated_values: list[tuple[int, int]] = []
        # A value of a condition folded into no JOIN has no reference - and binds nothing.
        optional_positions: set[int] = set()
        for position, origin in enumerate(origins):
            if origin[0] == OPTIONAL_VALUE_ORIGIN:
                origin = origins[position] = origin[1]
                optional_positions.add(position)
            first_position = first_position_by_origin.setdefault(origin, position)
            if first_position != position:
                repeated_values.append((position, first_position))
        value_sources: list[list[ValueReference]] = [[] for _origin in origins]
        for origin, reference in value_wrapper_references:
            value_position = first_position_by_origin.get(origin)
            if reference is None or value_position is None:
                return None
            value_sources[value_position].append(reference)
        if any(
            not references
            and position not in optional_positions
            and first_position_by_origin[origins[position]] == position
            for position, references in enumerate(value_sources)
        ):
            return None
        return tuple(tuple(references) for references in value_sources), tuple(repeated_values)

    @staticmethod
    def record_call_signature_plan(
        query: AwaitableQuery[Any], plan: StatementPlan, description: PlanDescription | None
    ) -> None:
        """Keeps the plan just found or built under the key of the calls the queryset was made
        with too, when its values are the calls' own.

        Args:
            query: The query.
            plan: The plan.
            description: The values the plan binds, each with its origin.
        """
        call_signature_record = query._call_signature_record
        if call_signature_record is None:
            return
        query._call_signature_record = None
        CallSignaturePlans.record(query, *call_signature_record, plan, description)
