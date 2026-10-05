from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.call_signatures.call_signature_plans import CallSignaturePlans
from hare.query.plans.constants import OPTIONAL_VALUE_ORIGIN
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup
from hare.query.statements.building.query_annotations import QueryAnnotations

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.value_references.value_reference_types import ValueReference
    from hare.query.plans.recording.plan_recording import PlanRecording
    from hare.query.plans.recording.scope_value_source import ScopeValueSource
    from hare.query.statements.awaitable_query import AwaitableQuery


class StatementPlanRuns:
    """How a query runs on a kept plan: the plan found under its description's key or under the key of
    the calls its queryset was made with, the query's values bound into the plan's statement, and
    the plan of a query just built kept for the later queries of its key."""

    @staticmethod
    def find_plan(
        query: AwaitableQuery[Any],
        plan_key: tuple[Any, ...],
        current_values: list[Any],
        *,
        paginate: bool = False,
        plan: StatementPlan | None = None,
    ) -> bool:
        """Runs this query on the plan kept under its key, when its values fit it. The output fields of
        ``populate_field_object`` annotations are restored too.

        Args:
            query: The query.
            plan_key: The plan key.
            current_values: This query's values, in the order its description lists them.
            paginate: Whether this query's LIMIT/OFFSET replace the plan's.
            plan: The plan when the caller already found it.

        Returns:
            True when this query runs on the plan - the caller then builds nothing.
        """
        if plan is None:
            plan = StatementPlans.find(plan_key)
        if plan is None or not query._runs_compiled_statement:
            return False
        parameters = StatementPlanRuns.get_plan_parameters(query, plan, current_values, paginate)
        if parameters is None:
            return False
        if query._call_signature_record is not None:
            # Kept under the key of the queryset's calls too, as a plan built now is.
            StatementPlanRuns.record_call_signature_plan(query, plan, query._get_plan_value_description(plan))
        return StatementPlanRuns.run_on_plan(query, plan, parameters)

    @staticmethod
    def get_plan_parameters(
        query: AwaitableQuery[Any], plan: StatementPlan, current_values: list[Any], paginate: bool
    ) -> list[Any] | None:
        """This query's parameters on a plan - its values, the values of the default scopes folded
        into the plan's JOINs, and its LIMIT/OFFSET.

        Args:
            query: The query.
            plan: The plan.
            current_values: This query's values, in the order its description lists them.
            paginate: Whether this query's LIMIT/OFFSET replace the plan's.

        Returns:
            The parameters, None when the query's SQL text would differ from the plan's.
        """
        if plan.scope_sources:
            scope_values = plan.get_scope_values()
            if scope_values is None:
                return None
            current_values = [*current_values, *scope_values]
        pagination = (query._limit, query._offset) if paginate else ()
        return plan.bind(current_values, query.model, query._connection.dialect, *pagination)

    @staticmethod
    def run_on_plan(query: AwaitableQuery[Any], plan: StatementPlan, parameters: list[Any]) -> bool:
        """Runs this query on a plan with its parameters bound - nothing is cloned or rendered.

        Args:
            query: The query.
            plan: The plan.
            parameters: The parameters (``StatementPlanRuns.get_plan_parameters()``).

        Returns:
            True - the query runs on the plan.
        """
        query.query = plan.query_builder
        query._compiled_statement = (plan.sql, parameters)  # type: ignore[assignment]
        query._annotation_output_fields = dict(plan.annotation_output_fields)
        query._statement_plan = plan
        StatementPlans.count_hit()
        return True

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
    ) -> None:
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
            return
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
                    "plan_recording": plan_recording,
                }
            return
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
                    return
                scope_sources.append(scope_source)
                scope_value_sources += [tuple(references) for references in references_by_position]
        value_sources = StatementPlanRuns.get_value_sources(description, value_wrapper_references)
        if value_sources is None:
            return
        description_value_sources, repeated_values = value_sources
        # A query with a plan that didn't run on it - built to be shown or built into another
        # query, or with a value the plan can't bind - keeps that plan.
        existing_plan = StatementPlans.find(plan_key)
        if existing_plan is not None:
            query._statement_plan = existing_plan
            StatementPlanRuns.record_call_signature_plan(query, existing_plan, description)
            return
        plan = query._statement_plan = StatementPlan(
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
        )
        StatementPlans.record(plan_key, plan)
        StatementPlanRuns.record_call_signature_plan(query, plan, description)

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
        CallSignaturePlans.record(query.model, *call_signature_record, plan, description)

    @staticmethod
    def run_on_call_signature_plan(query: AwaitableQuery[Any]) -> bool:
        """Runs this query on the plan kept under the key of the calls its queryset was made with
        (``CallSignaturePlans``).

        Args:
            query: The query.

        Returns:
            True when the query runs on the plan; False when it is built - the plan it builds is then
            kept under the key too.
        """
        CallsBeforeSetup.raise_if_invalid(query)
        query._call_signature_record = None
        type_structure, type_values = query._get_call_signature_type_part()
        if type_structure is None:
            return False
        signature_key = CallSignaturePlans.get_key(type(query), query, query._connection, type_structure)
        if signature_key is None:
            return False
        key, values, visibility = signature_key
        found = CallSignaturePlans.find_with_value_positions(query.model, key)
        if found is None:
            # The origins of the values - for the first query of the key alone.
            value_origins = CallSignaturePlans.get_value_origins(query, query._connection)
            if value_origins is not None:
                query._call_signature_record = (key, value_origins, len(type_values))
            return False
        plan = found[0]
        positions = found[1]
        bound_values = [*values, *type_values]
        if positions is not None:
            # The plan binds the values in another order.
            bound_values = [bound_values[position] for position in positions]
        parameters = StatementPlanRuns.get_plan_parameters(query, plan, bound_values, query.plan_binds_slice)
        query._deferred_plan_record = None
        if parameters is None or not StatementPlanRuns.run_on_plan(query, plan, parameters):
            return False
        query._visibility = visibility if visibility is not None else query._visibility.get_for_active_tenant()
        query._prepare_call_signature_run()
        query._restore_from_plan(plan)
        return True
