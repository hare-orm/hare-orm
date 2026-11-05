from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.lookup_info.value_paths import ValuePaths

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.expressions.expression_result import TableCriterionTuple
    from hare.sql.terms.field import Field as SqlField
    from hare.sql.terms.term import Term


@dataclass(frozen=True, slots=True)
class BoundLookupPath:
    """A model path bound to a table and the scopes of one expression build.

    ``columns`` records the physical inputs before field casts or value transforms.
    ``term`` and ``field`` describe the resulting value. Joins already contain ambient,
    visibility and explicit conditions; consumers only attach them to their statement.
    The context is retained for this build, never published as a reusable plan.
    """

    name: str
    context: ExpressionContext
    relations: tuple[RelationalField[Model], ...]
    columns: tuple[SqlField, ...]
    joins: tuple[TableCriterionTuple, ...]
    term: Term
    field: Field[Any] | None

    @property
    def crosses_to_many(self) -> bool:
        """Whether attaching this path can repeat a row of the starting model."""
        return any(relation.is_multi_valued for relation in self.relations)

    def get_result(self) -> ExpressionResult:
        """Expose the bound value to an expression without rebuilding its joins."""
        return ExpressionResult(term=self.term, joins=list(self.joins), output_field=self.field)

    @classmethod
    def bind(cls, context: ExpressionContext, name: str) -> BoundLookupPath:
        """Bind one scalar model path, including paths inside a field value.

        A terminal relation in an expression reads the joined row's key. Ordering
        expands relation keys before binding, so a forward relation orders by its
        local key even when the referenced row is hidden by a scope.
        """
        if (path_split := ValuePaths.get_value_path_split(context.model, name)) is not None:
            field_path, path_field, path_segments = path_split
            bound = cls.bind(context, field_path)
            term, output_field = ValuePaths.get_value_path_term(
                bound.term, path_field, path_segments, name, context.dialect
            )
            LookupPaths.mark_sensitive_column(term, path_field)
            return replace(bound, name=name, term=term, field=output_field)

        fields = LookupPaths.expand_expression(context.model, name)
        model = context.model
        table = context.table
        relations = []
        joins = []
        prefix = context.select_related_path_prefix
        last_field = fields[-1]
        for path_field in fields:
            if path_field.model_field_name not in model._meta.fetch_fields:
                break
            relation = cast("RelationalField[Model]", path_field)
            prefix = f"{prefix}__{relation.model_field_name}" if prefix else relation.model_field_name
            extra_conditions = context.select_related_extra_conditions
            new_joins = LookupPaths.get_scoped_joins(
                table,
                relation,
                relation.model_field_name,
                visibility=context.visibility,
                extra_condition=extra_conditions.get(prefix) if extra_conditions else None,
                dialect=context.dialect,
                connection=context.connection,
            )
            relations.append(relation)
            joins.extend(new_joins)
            table = new_joins[-1][0]
            model = relation.related_model

        if relations and relations[-1] is last_field:
            meta = model._meta
            if meta.has_composite_primary_key:
                raise QueryError(
                    f"{name!r} ends on a relation to a composite primary key - there's no single "
                    "column to read. Reference the individual components instead, e.g. "
                    f"'{name}__{meta.primary_key_attribute_names[0]}'."
                )
            column = table[
                meta.db_pk_column
                if meta.has_primary_key
                else cast("BackwardForeignKeyRelation[Model]", last_field).relation_source_fields[0]
            ]
        else:
            column = table[last_field.source_field or last_field.model_field_name]
        term = column
        if not (relations and relations[-1] is last_field):
            if function_cast := last_field.get_function_cast(context.dialect):
                term = function_cast(last_field, term)
        LookupPaths.mark_sensitive_column(term, last_field)
        return cls(name, context, tuple(relations), (column,), tuple(joins), term, last_field)
