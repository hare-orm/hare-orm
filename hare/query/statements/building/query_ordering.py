from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any

from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Expression, ExpressionContext
from hare.query.lookup_info.bound_lookup_path import BoundLookupPath
from hare.query.lookup_info.bound_ordering import BoundOrdering
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.sql import Order, Table
from hare.sql.functions.text.collate import Collate
from hare.sql.terms.field import Field
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.statements.awaitable_query import AwaitableQuery


class QueryOrdering:
    """The ORDER BY of a query: each ordering's terms - a relation's ordering key, an annotation, a
    path inside a value - with the placement of NULLs, and the ordering terms a DISTINCT needs
    selected."""

    @staticmethod
    def get_ordering(
        query: AwaitableQuery[Any],
        model: type[Model],
        table: Table,
        orderings: Iterable[tuple[str, Order]],
        annotations: dict[str, Term | Expression],
        fields_for_select: Collection[str] | None = None,
        annotation_output_aliases: dict[str, str] | None = None,
        *,
        select_related_path_prefix: str = "",
    ) -> None:
        """Attach ordering keys, binding model paths with their full relation scope.

        Selected annotations may use their output aliases when no comparison-key
        transformation is needed. Other annotations use their expression, including
        joins and values recorded by the annotation builder. A composite key expands
        into scalar ordering components before binding.
        """
        context = ExpressionContext(
            model=model,
            table=table,
            annotations=annotations,
            dialect=query.dialect,
            connection=query._connection,
            visibility=query._visibility,
            select_related_extra_conditions=query._select_related_extra_conditions,
            select_related_path_prefix=select_related_path_prefix,
        )
        for field_name, direction in query._apply_default_ordering(orderings):
            if field_name in annotations:
                if model is query.model:
                    EncryptedFieldBase.raise_if_encrypted(
                        QueryAnnotations.get_field_object_by_path(query, field_name), "ORDER BY"
                    )
                value_field = QueryAnnotations.get_annotation_value_field(query, field_name, annotations, table)
                is_selected = query.ordering_can_reference_annotation_alias and (
                    field_name not in query._alias_keys if not fields_for_select else field_name in fields_for_select
                )
                needs_expression = value_field is not None and (
                    isinstance(GeneratedField.get_effective_field(value_field), JSONField)
                    or query.dialect.types.has_ordering_key(value_field)
                )
                if is_selected and not needs_expression:
                    term: Term = Field((annotation_output_aliases or {}).get(field_name, field_name))
                else:
                    term = QueryAnnotations.get_annotation_expression_term(query, field_name, annotations, table)
                bound_ordering = BoundOrdering.bind(context, term, value_field, direction)
                if model is query.model:
                    query._annotation_ordering_terms[field_name] = bound_ordering.term
                if type(bound_ordering.term) is ValueWrapper:
                    # A number in ORDER BY may denote a selected column's position.
                    continue
                query.query = query.query.orderby(bound_ordering.term, order=direction)
                continue
            for path in ConcreteFieldPaths.get_paths(model, field_name):
                bound_path = BoundLookupPath.bind(context, path)
                for join in bound_path.joins:
                    QueryJoins.join_table(query, join)
                bound_ordering = BoundOrdering.bind(context, bound_path.term, bound_path.field, direction)
                query.query = query.query.orderby(bound_ordering.term, order=direction)

    @staticmethod
    def include_orderbys_in_select(query: AwaitableQuery[Any]) -> None:
        """Adds every ORDER BY term missing from the SELECT list to it - a plain ``SELECT DISTINCT``
        needs them selected. Applied on every dialect, so the rows are the same
        everywhere. A term added here is never read into a model attribute.

        Args:
            query: The query.
        """
        projection = DerivedProjection(query.query, output_fields=query._annotation_output_fields)
        features = query._connection.features if query._connection is not None else query.dialect.features
        orderbys: list[tuple[Term, Order | None]] = []
        for term, direction in query.query._orderbys:
            if features.collates_in_ordering_only and isinstance(term, Collate) and term.names_collation:
                projection.include(Collate.strip(term))
                orderbys.append((term, direction))
            elif isinstance(term, Field) and term.table is None and term.name in projection.by_alias:
                orderbys.append((term, direction))
            else:
                column = projection.include(term)
                # Reuse the selected expression, including its parameter sources.
                orderbys.append((column.term, direction))
        projection.query._orderbys = orderbys
        query.query = projection.query
