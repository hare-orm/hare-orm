from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions import Q
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.statements.building.query_joins import QueryJoins

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.statements.select.model_rows_query import ModelRowsQuery
    from hare.sql import Table


class SelectRelatedJoins:
    """The JOINs of select_related(): each relation joined with the columns its instance is read from,
    and the extra conditions of Select(relation, extra_condition=...) in the order the joins resolve
    them."""

    @staticmethod
    def join_select_related(
        query: ModelRowsQuery[Any],
        lookup_expression: str,
        value_wrapper_references: RecordedValueReferences | None = None,
    ) -> tuple[type[Model], Table]:
        fields = LookupPaths.expand_expression(query.model, lookup_expression)
        extra_condition = query._select_related_extra_conditions.get(lookup_expression)
        model: type[Model] = query.model
        table = query.model._meta.basetable
        path: tuple[str | None, ...] = (None,)
        for index, field in enumerate(fields):
            field = cast("RelationalField[Model]", field)
            path = path + (field.model_field_name,)
            is_last_field = index == len(fields) - 1
            table = QueryJoins.join_table_by_field(
                query,
                table,
                field.model_field_name,
                field,
                extra_condition if is_last_field else None,
                value_wrapper_references=value_wrapper_references,
            )

            # With a subset of fields selected, a relation's own fields are added only when
            # .select_related() asked for it and .only()/.defer() name none of its fields.
            step_lookup_expression = "__".join(cast("tuple[str, ...]", path[1:]))
            if query._effective_fields_for_select and (
                lookup_expression not in query._explicitly_select_related
                or any(
                    field_name.startswith(f"{step_lookup_expression}__")
                    for field_name in query._effective_fields_for_select
                )
            ):
                model = field.related_model
                continue

            related_fields = field.related_model._meta.db_fields
            append_item = (
                field.related_model,
                len(related_fields),
                field.model_field_name,
                model,
                path,
            )
            model = field.related_model
            if append_item in query._select_related_positions:
                # An earlier path already selects this hop - select_related("a", "a__b") walks
                # "a" twice; its columns are in the row once.
                continue
            query._select_related_positions.append(append_item)
            related_projection_reverse = field.related_model._meta.fields_db_projection_reverse
            query.query = query.query.select(
                *[
                    table[related_field].as_(
                        LookupPaths.safe_select_label(
                            table.get_table_name(), related_projection_reverse[related_field]
                        )
                    )
                    for related_field in related_fields
                ]
            )
        return model, table

    @staticmethod
    def select_related_extra_conditions_in_join_order(query: ModelRowsQuery[Any]) -> list[tuple[str, Q]]:
        """`(path, extra_condition)` pairs in the order `_build_query()`'s own
        `sorted(self._select_related)` loop resolves them in - the order their values are
        recorded in.

        Args:
            query: The model rows query.

        Returns:
            The pairs.
        """
        return [
            (path, query._select_related_extra_conditions[path])
            for path in sorted(query._select_related)
            if path in query._select_related_extra_conditions
        ]
