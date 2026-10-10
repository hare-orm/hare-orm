from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import ParameterPosition
from hare.fields.data.json.json_field import JSONField
from hare.fields.field import Field as ModelField
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F, Value
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.raw_sql import RawSQL
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.statements.building.query_joins import QueryJoins
from hare.sql import Table
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.awaitable_query import AwaitableQuery


class QueryAnnotations:
    """The annotations of a query: each resolved into its term and joins, selected under its key or
    kept as an alias, and the annotations a GROUP BY, ORDER BY or DISTINCT ON renders again."""

    @staticmethod
    def get_annotation_term(query: AwaitableQuery[Any], annotation: Expression | Term) -> Term:
        """The term an annotation resolves to, against the query's own annotations.

        Args:
            query: The query.
            annotation: The annotation.

        Returns:
            The term.
        """
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            return annotation
        return annotation.get_result(query._get_probe_expression_context(query._annotations)).term

    @staticmethod
    def annotation_is_aggregate(query: AwaitableQuery[Any], annotation: Expression | Term) -> bool:
        """Whether an annotation resolves to an aggregate term (``Count(...)``,
        ``Coalesce(Sum(...), 0)``, an arithmetic expression over one, ...).

        Args:
            query: The query.
            annotation: The annotation.

        Returns:
            True for an aggregate.
        """
        return QueryAnnotations.get_annotation_term(query, annotation).contains_aggregate

    @staticmethod
    def get_annotate(
        query: AwaitableQuery[Any],
        fields_for_select: Collection[str] | None = None,
        *,
        value_wrapper_references: (RecordedValueReferences | None) = None,
        aggregated_multi_valued_paths: AggregatedMultiValuedPaths | None = None,
    ) -> bool:
        # Fresh on every build - kept on the query, not on the annotation expression, which queries
        # share.
        query._annotation_output_fields = {}
        if not query._annotations:
            return False

        has_aggregate = False
        unused_alias_keys = query._get_unused_alias_keys(fields_for_select)
        # In dict order - the order of the .annotate() calls, which is the SELECT column order and
        # the order the plan lists the values in. An expression registered under several keys is
        # resolved once.
        results_by_annotation_id: dict[int, ExpressionResult] = {}
        for key, annotation in query._annotations.items():
            if key in unused_alias_keys:
                continue
            earlier_info = results_by_annotation_id.get(id(annotation))
            # A Subquery is a Term and an Expression: its inner query is built while this query's
            # context is active, or its OuterReference finds nothing.
            if earlier_info is not None:
                info = earlier_info
            elif isinstance(annotation, Term) and not isinstance(annotation, Expression):
                info = ExpressionResult(term=annotation)
                if isinstance(annotation, RawSQL) and value_wrapper_references is not None:
                    # RawSQL wrapped its params when it was created - recorded as they are.
                    ExpressionArguments.record_parameters(
                        value_wrapper_references, annotation, "parameters", annotation.parameters
                    )
            else:
                info = annotation.get_result(
                    ExpressionContext(
                        model=query.model,
                        dialect=query.dialect,
                        connection=query._connection,
                        # The table the FROM clause uses - an alias for a self-referential model
                        # inside a subquery.
                        table=query._effective_basetable(),
                        annotations=query._annotations,
                        value_wrapper_references=value_wrapper_references,
                        annotation_names_in_progress={key},
                        # An annotation crossing a select_related(extra_condition=...) relation
                        # builds its JOIN with that condition - the JOIN built first is the one
                        # kept.
                        select_related_extra_conditions=query._select_related_extra_conditions,
                        aggregated_multi_valued_paths=aggregated_multi_valued_paths,
                        visibility=query._visibility,
                    )
                )
            results_by_annotation_id[id(annotation)] = info

            for join in info.joins:
                QueryJoins.join_table(query, join)
            # An .alias() key isn't selected - unless .values()/.values_list() names it. Without
            # them every ordinary annotation is.
            should_select = key in fields_for_select if fields_for_select is not None else key not in query._alias_keys
            if should_select:
                select_term = info.term
                if isinstance(annotation, Value):
                    select_term = Value.get_typed_term(
                        select_term, annotation.value, ParameterPosition.SELECTED_VALUE, query.dialect
                    )
                query.query._select_other(select_term.as_(key))  # type:ignore[arg-type]
            has_aggregate = has_aggregate or info.term.contains_aggregate
            # The result's field is kept on this build, not on the annotation expression queries
            # share - only for functions whose result has their argument's type
            # (populate_field_object).
            if getattr(annotation, "populate_field_object", False):
                query._annotation_output_fields[key] = (
                    annotation.get_value_field(info) if isinstance(annotation, Expression) else info.output_field  # type:ignore[call-overload]
                )
            elif isinstance(annotation, Value):
                query._annotation_output_fields[key] = Value.get_literal_output_field(annotation.value)

        return has_aggregate

    @staticmethod
    def get_annotation_expression_term(
        query: AwaitableQuery[Any], field_name: str, annotations: dict[str, Any], table: Table
    ) -> Term:
        """Resolves an annotation to its full SQL expression, for a clause that cannot reference its SELECT alias.

        Args:
            query: The query.
            field_name: The annotation name.
            annotations: The annotations the expression may reference.
            table: The table the expression is resolved against.

        Returns:
            The annotation's expression term.
        """
        annotation = annotations[field_name]
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            return annotation
        cache_key = (field_name, table)
        if (term := query._annotation_expression_terms.get(cache_key)) is not None:
            return term
        # The expression's values are recorded apart from the query's own - the plan binds them
        # last (QueryAnnotations.get_expression_term_value_references()). Resolved for a second table, the name records
        # more references than its values, and the query keeps no plan.
        value_wrapper_references = query._expression_term_references.setdefault(field_name, [])
        term = annotation.get_result(
            QueryAnnotations.get_annotation_expression_context(
                query, field_name, annotations, table, value_wrapper_references
            )
        ).term
        query._annotation_expression_terms[cache_key] = term
        return term

    @staticmethod
    def get_annotation_expression_context(
        query: AwaitableQuery[Any],
        field_name: str,
        annotations: dict[str, Any],
        table: Table,
        value_wrapper_references: RecordedValueReferences | None = None,
    ) -> ExpressionContext:
        """The context an annotation is resolved in outside SELECT.

        Args:
            query: The query.
            field_name: The annotation name.
            annotations: The annotations the expression may reference.
            table: The table the expression is resolved against.
            value_wrapper_references: Where the expression's value references are recorded.

        Returns:
            The resolve context.
        """
        return ExpressionContext(
            model=query.model,
            dialect=query.dialect,
            connection=query._connection,
            table=table,
            annotations=annotations,
            annotation_names_in_progress={field_name},
            visibility=query._visibility,
            value_wrapper_references=value_wrapper_references,
        )

    @staticmethod
    def get_expression_term_names(query: AwaitableQuery[Any]) -> list[str]:
        """The annotations a GROUP BY, ORDER BY or DISTINCT ON names - each may be resolved again
        into its full expression there (``QueryAnnotations.get_annotation_expression_term()``).

        Args:
            query: The query.

        Returns:
            The names, sorted.
        """
        annotations = query._annotations
        if not annotations:
            return []
        named = {*query._group_bys, *(ordering[0] for ordering in query._orderings), *query._distinct_on}
        return sorted(name for name in named if name in annotations)

    @staticmethod
    def get_expression_term_value_references(query: AwaitableQuery[Any]) -> RecordedValueReferences:
        """The value references of the annotations a GROUP BY, ORDER BY or DISTINCT ON names, in
        the order of their names - one the build didn't resolve again (selected under its alias)
        is resolved now, its references then binding nothing: the text doesn't hold its terms.

        Args:
            query: The query.

        Returns:
            The references.
        """
        value_wrapper_references: RecordedValueReferences = []
        names = QueryAnnotations.get_expression_term_names(query)
        if not names:
            return value_wrapper_references
        table = query._effective_basetable()
        for name in names:
            annotation = query._annotations[name]
            if isinstance(annotation, RawSQL):
                ExpressionArguments.record_parameters(
                    value_wrapper_references, annotation, "parameters", annotation.parameters
                )
                continue
            if name not in query._expression_term_references:
                QueryAnnotations.get_annotation_expression_term(query, name, query._annotations, table)
            value_wrapper_references.extend(query._expression_term_references.get(name, ()))
        return value_wrapper_references

    @staticmethod
    def annotation_holds_json(
        query: AwaitableQuery[Any], field_name: str, annotations: dict[str, Any], table: Table
    ) -> bool:
        """Whether an annotation's value is JSON (a JSON field, a path into one, a ``JSONObject``).

        Args:
            query: The query.
            field_name: The annotation name.
            annotations: The annotations the expression may reference.
            table: The table the expression is resolved against.

        Returns:
            True for a JSON value.
        """
        annotation = annotations[field_name]
        if not isinstance(annotation, Expression):
            return False
        result = annotation.get_result(
            QueryAnnotations.get_annotation_expression_context(query, field_name, annotations, table)
        )
        return isinstance(annotation.get_value_field(result), JSONField)

    @staticmethod
    def get_annotation_select_alias(query: AwaitableQuery[Any], field_name: str) -> str | None:
        """Finds the alias an annotation is actually SELECTed under.

        Args:
            query: The query.
            field_name: The annotation name.

        Returns:
            The SELECT alias (the name itself, or a positional/renamed alias the same annotation
            was re-registered under by values()/values_list()), or None when it is not selected.
        """
        selected_aliases = {select.alias for select in query.query._selects if select.alias}
        if field_name in selected_aliases:
            return field_name
        annotation = query._annotations[field_name]
        for alias in selected_aliases:
            if query._annotations.get(alias) is annotation:
                return alias
        return None

    @staticmethod
    def get_field_object_by_path(query: AwaitableQuery[Any], field_name: str) -> ModelField[Any] | None:
        """Returns the model field a ``field``/``relation__field`` name (or an annotation that's a
        bare ``F()`` of one) points at.

        Args:
            query: The query.
            field_name: The field path or annotation name.

        Returns:
            The field, or None when the name isn't a plain model field path.
        """
        seen_annotation_names: set[str] = set()
        while (annotation := query._annotations.get(field_name)) is not None:
            if type(annotation) is not F or field_name in seen_annotation_names:
                return None
            seen_annotation_names.add(field_name)
            field_name = annotation.name
        concrete_field_paths = ConcreteFieldPaths.get_paths(query.model, field_name)
        if len(concrete_field_paths) == 1:
            field_name = concrete_field_paths[0]
        return LookupPath.parse(query.model, field_name).get_target_field()

    @staticmethod
    def get_annotation_fields(query: AwaitableQuery[Any]) -> dict[str, ModelField[Any]]:
        """The field each ``populate_field_object`` annotation's value is decoded through, by key - a
        model instance decodes such an annotation the way ``.values()`` does.

        Args:
            query: The query.
        """
        return {
            key: output_field
            for key, output_field in query._annotation_output_fields.items()
            if output_field is not None
        }
