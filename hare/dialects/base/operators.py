from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.filters.field_lookup import FieldLookup
    from hare.sql.terms.base.term import Term
    from hare.sql.terms.criteria.criterion import Criterion


class FilterOperators:
    """The operators a dialect runs lookups with - a lookup's own operator, or the dialect's
    replacement for it."""

    def __init__(self, overrides: dict[Callable[..., Any], Callable[..., Any]]) -> None:
        self.overrides = overrides

    def get_overridden_operator(
        self, operator: Callable[..., Any], field_lookup: FieldLookup | None
    ) -> Callable[..., Any] | None:
        """The dialect's replacement for a lookup operator.

        Args:
            operator: The lookup's own operator.
            field_lookup: The lookup.

        Returns:
            The replacement, or None when the lookup's own operator runs.
        """
        return self.overrides.get(operator)

    def supports_operator(self, field_lookup: FieldLookup) -> bool:
        """Whether this dialect runs a lookup's operator - one only dialects implement (marked by
        ``DialectImplementedOperators``) has to be replaced here.

        Args:
            field_lookup: The lookup.

        Returns:
            True when the lookup's operator runs on this dialect.
        """
        # Local import: the filters package imports the dialects package.
        from hare.query.filters.dialect_implemented_operators import DialectImplementedOperators

        operator = field_lookup.operator
        return (
            not DialectImplementedOperators.is_marked(operator)
            or self.get_overridden_operator(operator, field_lookup) is not None
        )

    def get_operator(self, field_lookup: FieldLookup) -> Callable[..., Any]:
        """The operator a lookup runs on this dialect, with the lookup's text function applied.

        Args:
            field_lookup: The lookup.

        Returns:
            The operator.
        """
        operator = self.get_overridden_operator(field_lookup.operator, field_lookup) or field_lookup.operator
        if field_lookup.text_function is not None:
            operator = partial(operator, text_function=field_lookup.text_function)
        return operator

    def get_row_membership_criterion(
        self,
        columns: Sequence[Term],
        value_rows: Sequence[Sequence[Any]],
        array_element_fields: Sequence[Field[Any] | None] | None = None,
    ) -> Criterion:
        """``columns IN value_rows`` through the dialect's ``__in`` operator, so a long list binds
        as few parameters as the dialect allows.

        Args:
            columns: The compared columns, one per value in each row.
            value_rows: The DB-ready value rows.
            array_element_fields: Per column, its field, or None to type it by its values.

        Returns:
            The criterion.
        """
        # Local import: the filters package imports the dialects package.
        from hare.query.filters import Lookups
        from hare.query.filters.field_lookup import FieldLookup
        from hare.sql.terms.tuple import Tuple

        if len(columns) == 1:
            single_column_lookup = FieldLookup(
                Lookups.is_in, array_element_field=array_element_fields[0] if array_element_fields else None
            )
            in_operator = self.get_overridden_operator(Lookups.is_in, single_column_lookup) or Lookups.is_in
            return in_operator(columns[0], [row[0] for row in value_rows])
        row_lookup = FieldLookup(
            Lookups.row_is_in,
            array_element_fields=None if array_element_fields is None else tuple(array_element_fields),
        )
        row_operator = self.get_overridden_operator(Lookups.row_is_in, row_lookup) or Lookups.row_is_in
        return row_operator(Tuple(*columns), [tuple(row) for row in value_rows])
