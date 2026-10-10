from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.expressions.subqueries.outer_query_state import outer_expression_context
from hare.query.plans.description.hashable_values import HashableValues
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.query_connection import QueryConnection
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.conditions.q import Q
    from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
    from hare.query.statements.awaitable_query import AwaitableQuery


class StatementPlanDescriptions:
    """How a query describes the plan it keeps: the structure - the plan key - and the values a plan
    binds, of its annotations, filters, keyset boundaries, CTEs, dialect method calls and the
    expressions a GROUP BY/ORDER BY resolves again; and whether its state allows a plan at all. The
    slots every type of query declares alike (``plan_slots``) are kept here too."""

    #: The description of a query without keyset boundaries (``get_cursor_plan_description()``).
    NO_CURSOR_PLAN_DESCRIPTION: ClassVar[PlanDescription] = PlanDescription(((), ()), [])

    #: The slots a query's plan key starts with - the model, the connection and the visibility.
    HEAD_SLOTS: ClassVar[DeclaredPlanSlots]
    #: The slots of the rows a query picks - its annotations and filters, its distinct settings,
    #: the settings written into its SQL text, its CTEs, its ``Select(extra_condition=...)``
    #: conditions, the dialect's QuerySet method calls and the zone of aware datetimes.
    ROWS_SLOTS: ClassVar[DeclaredPlanSlots]
    #: The slot of the keyset boundaries.
    CURSOR_SLOTS: ClassVar[DeclaredPlanSlots]
    #: The slot of the annotations a GROUP BY/ORDER BY/DISTINCT ON resolves again - of a type binding
    #: their values (``binds_expression_term_values``).
    EXPRESSION_TERMS_SLOTS: ClassVar[DeclaredPlanSlots]

    @staticmethod
    def query_is_plannable(query: AwaitableQuery[Any]) -> bool:
        """Whether this query keeps a plan: its state allows one and each annotation and filter keeps
        one.

        Args:
            query: The query.

        Returns:
            True when the query keeps a plan.
        """
        return (
            StatementPlanDescriptions.query_state_is_plannable(query)
            and StatementPlanDescriptions.get_filters_plan_description(query) is not None
        )

    @staticmethod
    def query_state_is_plannable(query: AwaitableQuery[Any]) -> bool:
        """Whether the state the description doesn't cover allows a plan: no table sample, no grouping or
        ordering by an annotation rendering values of its own, every ``.alias()`` used, and no
        correlation to an enclosing query on the same table. Checked before the build.

        Args:
            query: The query.

        Returns:
            True when the state allows a plan.
        """
        if query._options.keeps_no_plan():
            # The sample's percent and seed are written into FROM, not bound.
            return False
        outer_context = outer_expression_context.get()
        outer_table_name = outer_context.table.get_table_name() if outer_context is not None else None
        if outer_table_name is not None and query.model._meta.basetable.get_table_name() == outer_table_name:
            return False
        annotations = query._annotations
        if not annotations:
            return True
        # A GROUP BY/ORDER BY of an annotation that isn't SELECTed, and a DISTINCT ON of one,
        # renders the annotation's full expression - a type that doesn't bind its values
        # (binds_expression_term_values) would keep the first query's values there.
        if not query.binds_expression_term_values:
            for name in QueryAnnotations.get_expression_term_names(query):
                annotation = annotations[name]
                if not isinstance(annotation, Plannable) or (
                    name not in query._distinct_on and not query._annotation_renders_as_expression(name)
                ):
                    continue
                annotation_description = annotation.get_plan_description(PlanContext.EMPTY)
                if annotation_description is not None and annotation_description.values:
                    return False
        # An unused .alias() is left out of the built query, so the values it holds would no
        # longer line up with every annotation's - one holding none (a renamed field) is harmless.
        for name in query._get_unused_alias_keys():
            annotation = annotations[name]
            if not isinstance(annotation, Plannable):
                return False
            annotation_description = annotation.get_plan_description(PlanContext.EMPTY)
            if annotation_description is None or annotation_description.values:
                return False
        return True

    @staticmethod
    def get_annotations_plan_description(query: AwaitableQuery[Any]) -> PlanDescription | None:
        """Describes the annotations in the order ``QueryAnnotations.get_annotate()`` resolves them: per key its name,
        its expression's structure and whether it is an ``.alias()``. An expression under several
        keys is listed once.

        Args:
            query: The query.

        Returns:
            The description, None when an annotation keeps no plan.
        """
        annotations = query._annotations
        if not annotations:
            return PlanDescription.EMPTY
        context = PlanContext(annotations, model=query.model)
        structures: list[tuple[Any, ...]] = []
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        descriptions_by_annotation_id: dict[int, tuple[str, PlanDescription]] = {}
        for key, annotation in annotations.items():
            first_key_and_description = descriptions_by_annotation_id.get(id(annotation))
            if first_key_and_description is None:
                if not isinstance(annotation, Plannable):
                    return None
                description = annotation.get_plan_description(context)
                if description is None:
                    return None
                first_key_and_description = descriptions_by_annotation_id[id(annotation)] = (key, description)
                values.extend(description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(description, origins)
            first_key, description = first_key_and_description
            structures.append((key, description.structure, key in query._alias_keys, first_key))
        return PlanDescription(tuple(structures), values, origins)

    @staticmethod
    def get_conditions_plan_description(conditions: Iterable[Q], context: PlanContext) -> PlanDescription | None:
        """Describes conditions applied one after another - a query's filters, a relation's
        ``Select(extra_condition=...)``.

        Args:
            conditions: The conditions.
            context: The context they are resolved in.

        Returns:
            The structure of each and the values of all of them in order, None when one keeps no
            plan.
        """
        structures = []
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for condition in conditions:
            description = condition.get_plan_description(context)
            if description is None:
                return None
            structures.append(description.structure)
            values.extend(description.values)
            if origins is not None:
                origins = PlanParts.get_origins(description, origins)
        return PlanDescription(tuple(structures), values, origins)

    @staticmethod
    def get_filters_plan_description(query: AwaitableQuery[Any]) -> PlanDescription | None:
        """Describes the annotations, then the filters - ``QueryConditions.get_filters()`` resolves them in this
        order; a filter on an annotation resolves the annotation again.

        Args:
            query: The query.

        Returns:
            The description, None when an annotation or a filter keeps no plan.
        """
        annotations_description = StatementPlanDescriptions.get_annotations_plan_description(query)
        if annotations_description is None:
            return None
        # A query built into another one has no connection of its own yet - its lists are described
        # by their length.
        connection = query._connection
        filters_description = StatementPlanDescriptions.get_conditions_plan_description(
            query._q_objects,
            PlanContext(
                query._annotations or None,
                connection.dialect.parameters.single_parameter_in_list_min_length if connection is not None else None,
                model=query.model,
                describes_json_containment_by_shape=(
                    connection is None or connection.dialect.parameters.describes_json_containment_by_shape
                ),
            ),
        )
        if filters_description is None:
            return None
        return PlanDescription(
            (annotations_description.structure, filters_description.structure),
            annotations_description.values + filters_description.values,
            PlanParts.get_origins(filters_description, PlanParts.get_origins(annotations_description, []))
            if PlanOrigins.records
            else None,
        )

    @staticmethod
    def get_extra_conditions_plan_description(query: AwaitableQuery[Any]) -> PlanDescription | None:
        """Describes the ``Select(relation, extra_condition=...)`` conditions - each folded into the
        JOIN of the relation wherever the query crosses it, its values bound there: none of a relation
        the query doesn't cross.

        Args:
            query: The query.

        Returns:
            The relations' paths and the conditions' structures, and their values - None when a
            condition keeps no plan.
        """
        extra_conditions = query._select_related_extra_conditions
        if not extra_conditions:
            return PlanDescription.EMPTY
        description = StatementPlanDescriptions.get_conditions_plan_description(
            extra_conditions.values(), PlanContext.EMPTY
        )
        if description is None:
            return None
        return PlanDescription(
            (tuple(extra_conditions), description.structure),
            description.values,
            PlanParts.get_optional_origins(description, []) if PlanOrigins.records else None,
        )

    @staticmethod
    def get_cursor_plan_description(query: AwaitableQuery[Any]) -> PlanDescription:
        """Describes the keyset boundaries: per bounded field whether its value is None - an ``IS
        NULL`` test instead of a comparison - and the values other than None, lower boundary first.

        Args:
            query: The query.

        Returns:
            The description.
        """
        cursor_values = query._cursor_values
        before_cursor_values = query._before_cursor_values
        if not cursor_values and not before_cursor_values:
            return StatementPlanDescriptions.NO_CURSOR_PLAN_DESCRIPTION
        return PlanDescription(
            (
                tuple(value is None for value in cursor_values),
                tuple(value is None for value in before_cursor_values),
            ),
            [value for value in (*cursor_values, *before_cursor_values) if value is not None],
            [
                *(
                    PlanOrigins.get_value_origin(query, "_cursor_values", index)
                    for index, value in enumerate(cursor_values)
                    if value is not None
                ),
                *(
                    PlanOrigins.get_value_origin(query, "_before_cursor_values", index)
                    for index, value in enumerate(before_cursor_values)
                    if value is not None
                ),
            ]
            if PlanOrigins.records
            else None,
        )

    @staticmethod
    def get_ctes_plan_description(query: AwaitableQuery[Any]) -> PlanDescription | None:
        """Describes the CTEs: each one's name and its body's description, the query built into
        this one (``QueryCtes.apply_with_ctes(value_wrapper_references=...)`` records the bodies' values).

        Args:
            query: The query.

        Returns:
            The description, None when a body keeps no plan - a body given as SQL among them.
        """
        options = query._options
        if options is QueryOptions.DEFAULT or not options.with_ctes:
            return PlanDescription.EMPTY
        # Local import: the query class imports this module.
        from hare.query.statements.awaitable_query import AwaitableQuery

        structures: list[tuple[Any, ...]] = []
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for name, cte_body in options.with_ctes:
            if not isinstance(cte_body, AwaitableQuery):
                return None
            cte_description = cte_body.get_plan_description(PlanContext.EMPTY)
            if cte_description is None:
                return None
            structures.append((name, QueryConnection.get_pinned_connection_name(cte_body), cte_description.structure))
            values.extend(cte_description.values)
            if origins is not None:
                origins = PlanParts.get_origins(cte_description, origins)
        return PlanDescription(tuple(structures), values, origins)

    @staticmethod
    def get_extension_calls_structure(query: AwaitableQuery[Any]) -> tuple[Any, ...] | None:
        """The calls of the dialect's QuerySet methods this query makes (.final(), .prewhere(), ...),
        for its plan key (``get_extension_calls_plan_description()``).

        Args:
            query: The query.

        Returns:
            The structure, or None when an argument can't be part of a key - such a query keeps no
            plan.
        """
        description = StatementPlanDescriptions.get_extension_calls_plan_description(query)
        return None if description is None else description.structure

    @staticmethod
    def get_extension_calls_plan_description(query: AwaitableQuery[Any]) -> PlanDescription | None:
        """Describes the calls of the dialect's QuerySet methods this query makes: each method's name
        and arguments - the dialect writes them into the query after it is built, so they are part
        of its SQL text. An argument that describes itself (a ``Q``, an expression) is described as
        a filter is, its values bound; any other argument is part of the key as it is.

        Args:
            query: The query.

        Returns:
            One ``(name, args, kwargs)`` structure per call and the values of the arguments
            describing themselves, None when an argument can't be part of a key (it isn't hashable,
            or keeps no plan).
        """
        options = query._options
        if options is QueryOptions.DEFAULT or not options.extension_calls:
            return PlanDescription.EMPTY
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        context: PlanContext | None = None
        structures = []
        for extension_call in options.extension_calls:
            arguments = []
            for argument in (*extension_call.args, *extension_call.kwargs.values()):
                if not isinstance(argument, Plannable):
                    arguments.append(argument)
                    continue
                if context is None:
                    connection = query._connection
                    context = PlanContext(
                        query._annotations or None,
                        connection.dialect.parameters.single_parameter_in_list_min_length
                        if connection is not None
                        else None,
                        model=query.model,
                        describes_json_containment_by_shape=(
                            connection is None or connection.dialect.parameters.describes_json_containment_by_shape
                        ),
                    )
                description = argument.get_plan_description(context)
                if description is None:
                    return None
                arguments.append((Plannable, description.structure))
                values.extend(description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(description, origins)
            structures.append((extension_call.name, tuple(extension_call.kwargs), tuple(arguments)))
        structure = tuple(structures)
        try:
            hash(structure)
        except TypeError:
            return None
        return PlanDescription(structure, values, origins)

    @staticmethod
    def get_options_structure(query: AwaitableQuery[Any]) -> tuple[Any, ...]:
        """The part of a plan key the settings written into the SQL text make - DISTINCT ON, the
        grouping, the row locks, ...

        Args:
            query: The query.

        Returns:
            The part.
        """
        return query._options.get_plan_key_part()

    @staticmethod
    def get_zone_structure(query: AwaitableQuery[Any]) -> str | None:
        """The zone a ``__year``/``__month``/... lookup on a datetime renders into its criterion,
        not a bound value.

        Args:
            query: The query.

        Returns:
            The zone's name.
        """
        return Timezone.get_rendered_zone_name()

    @staticmethod
    def get_visibility_structure(query: AwaitableQuery[Any]) -> tuple[Any, ...]:
        """The part of a plan key the query's visibility decides - its escape hatches and a tenant
        it pins. The active tenant isn't part of it: a plan binds the default scopes of the
        running query's context.

        Args:
            query: The query.

        Returns:
            The parts.
        """
        visibility = query._visibility
        return (
            visibility.all_tenants,
            visibility.include_deleted,
            visibility.only_deleted,
            HashableValues.get_hashable_value(visibility.tenant) if visibility.tenant_is_pinned else None,
        )

    @staticmethod
    def get_expression_terms_plan_description(query: AwaitableQuery[Any]) -> PlanDescription | None:
        """Describes the annotations a GROUP BY, ORDER BY or DISTINCT ON names, for a type that
        binds their expressions' values (``binds_expression_term_values``) - by their names: their
        values are bound where the annotations are described.

        Args:
            query: The query.

        Returns:
            The description.
        """
        names = QueryAnnotations.get_expression_term_names(query)
        if not names:
            return PlanDescription.EMPTY
        return PlanDescription(tuple(names), [], [])


StatementPlanDescriptions.HEAD_SLOTS = (
    ("model", PlanKeyForm.VALUE),
    ("_connection", PlanKeyForm.CONNECTION),
    (StatementPlanDescriptions.get_visibility_structure, PlanKeyForm.METHOD),
)
StatementPlanDescriptions.ROWS_SLOTS = (
    (StatementPlanDescriptions.get_filters_plan_description, PlanKeyForm.DESCRIBED),
    ("_distinct", PlanKeyForm.VALUE),
    (StatementPlanDescriptions.get_options_structure, PlanKeyForm.METHOD),
    (StatementPlanDescriptions.get_ctes_plan_description, PlanKeyForm.DESCRIBED),
    (StatementPlanDescriptions.get_extra_conditions_plan_description, PlanKeyForm.DESCRIBED),
    (StatementPlanDescriptions.get_extension_calls_plan_description, PlanKeyForm.DESCRIBED),
    (StatementPlanDescriptions.get_zone_structure, PlanKeyForm.METHOD),
)
StatementPlanDescriptions.CURSOR_SLOTS = (
    (StatementPlanDescriptions.get_cursor_plan_description, PlanKeyForm.DESCRIBED),
)
StatementPlanDescriptions.EXPRESSION_TERMS_SLOTS = (
    (StatementPlanDescriptions.get_expression_terms_plan_description, PlanKeyForm.DESCRIBED),
)
