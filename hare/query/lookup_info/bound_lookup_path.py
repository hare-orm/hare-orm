from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.lookup_info.value_paths import ValuePaths

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hare.fields.field import Field
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.expressions.expression_result import TableCriterionTuple
    from hare.sql import Table
    from hare.sql.terms.field import Field as SqlField
    from hare.sql.terms.term import Term


@dataclass(frozen=True, slots=True)
class BoundLookupPath:
    """A model path bound to a table and the scopes of one expression build.

    ``column`` is the physical input before field casts or value transforms.
    ``term`` and ``field`` describe the resulting value. Joins already contain ambient,
    visibility and explicit conditions; consumers only attach them to their statement.
    The bound path belongs to this build, never published as a reusable plan.
    """

    column: SqlField
    joins: tuple[TableCriterionTuple, ...]
    term: Term
    field: Field[Any] | None

    def get_result(self) -> ExpressionResult:
        """Expose the bound value to an expression without rebuilding its joins."""
        return ExpressionResult(term=self.term, joins=list(self.joins), output_field=self.field)

    @classmethod
    def get_joined_result(
        cls,
        context: ExpressionContext,
        binding: ModelBinding,
        table: Table,
        joins: Sequence[TableCriterionTuple],
        field_path: str,
    ) -> ExpressionResult:
        """Read a path from an already joined table in the same expression build."""
        joined_context = replace(
            context,
            binding=binding,
            table=table,
            annotations={},
            select_related_extra_conditions=None,
            select_related_path_prefix="",
        )
        bound = cls.bind(joined_context, field_path)
        return ExpressionResult(term=bound.term, joins=[*joins, *bound.joins], output_field=bound.field)

    @classmethod
    def bind_value_path(
        cls, context: ExpressionContext, name: str, path_split: tuple[str, Field[Any], list[str]]
    ) -> BoundLookupPath:
        """Bind an already parsed path inside a field value without discovering it again."""
        field_path, path_field, path_segments = path_split
        bound = cls.bind(context, field_path)
        term, output_field = ValuePaths.get_value_path_term(
            bound.term, path_field, path_segments, name, context.dialect
        )
        LookupPaths.mark_sensitive_column(term, path_field)
        return replace(bound, term=term, field=output_field)

    @classmethod
    def bind(cls, context: ExpressionContext, name: str) -> BoundLookupPath:
        """Bind one scalar model path, including paths inside a field value.

        A terminal relation in an expression reads the joined row's key. Ordering
        expands relation keys before binding, so a forward relation orders by its
        local key even when the referenced row is hidden by a scope.
        """
        if (path_split := ValuePaths.get_value_path_split(context.binding, name)) is not None:
            return cls.bind_value_path(context, name, path_split)
        lookup_path, last_field = LookupPaths.get_expression_path(context.binding, name)
        relations = lookup_path.relations
        ends_in_relation = last_field.model_field_name in lookup_path.binding.fetch_fields
        if ends_in_relation:
            terminal_relation: RelationalField[Model] = last_field  # type: ignore[assignment]
            relations += (terminal_relation,)
        table = context.table
        joins = []
        prefix = context.select_related_path_prefix
        for relation in relations:
            path_field_name = relation.model_field_name
            prefix = f"{prefix}__{path_field_name}" if prefix else path_field_name
            extra_conditions = context.select_related_extra_conditions
            new_joins = LookupPaths.get_scoped_joins(
                table,
                relation,
                path_field_name,
                visibility=context.visibility,
                extra_condition=extra_conditions.get(prefix) if extra_conditions else None,
                dialect=context.dialect,
                connection=context.connection,
                application_context=context.application_context,
            )
            joins.extend(new_joins)
            table = new_joins[-1][0]

        if ends_in_relation:
            terminal_relation = relations[-1]
            meta = terminal_relation.get_related_binding()
            if meta.has_composite_primary_key:
                raise QueryError(
                    f"{name!r} ends on a relation to a composite primary key - there's no single "
                    "column to read. Reference the individual components instead, e.g. "
                    f"'{name}__{meta.primary_key_attribute_names[0]}'."
                )
            column = table[meta.db_pk_column if meta.has_primary_key else terminal_relation.relation_source_fields[0]]  # type: ignore[attr-defined]
        else:
            column = table[last_field.source_field or last_field.model_field_name]
        term: Term = column
        if not ends_in_relation:
            if function_cast := last_field.get_function_cast(context.dialect):
                term = function_cast(last_field, term)
        LookupPaths.mark_sensitive_column(term, last_field)
        return cls(column, tuple(joins), term, last_field)
