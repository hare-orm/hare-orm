from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.not_criterion import Not
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple


class LargeInList:
    """``__in``/``__not_in`` for a value list too long to bind one parameter per value: the dialect
    binds the values (or the value rows) as one parameter a container of its own reads, and NULLs
    are compared apart. A list the container can't carry exactly keeps the plain ``IN (...)`` form.
    """

    def get_membership_criterion(
        self, term: Term, values: Sequence[Any], non_null_values: list[Any], element_type: str | None
    ) -> Criterion | None:
        """``term`` among the non-None values, bound as one parameter.

        Args:
            term: The compared term.
            values: The whole lookup value.
            non_null_values: Its values other than None.
            element_type: The compared term's SQL type, None to type by the values.

        Returns:
            The criterion, None when the plain ``IN (...)`` form is kept.
        """
        raise NotImplementedError

    def get_row_container(
        self, value_rows: list[tuple[Any, ...]], element_types: Sequence[str | None] | None
    ) -> Term | None:
        """The one-parameter container a row-value membership check reads.

        Args:
            value_rows: The value rows, each holding one value per compared column.
            element_types: Per column, its SQL type, None to type it by its values.

        Returns:
            The container, None when the plain row list is kept.
        """
        raise NotImplementedError

    def get_criterion(self, term: Term, value: Any, element_type: str | None) -> tuple[Criterion, bool] | None:
        """The membership criterion of a list and whether the list holds a None.

        Args:
            term: The compared term.
            value: The encoded lookup value.
            element_type: The compared term's SQL type, None to type by the values.

        Returns:
            The criterion and the flag, None when the plain ``IN (...)`` form is kept.
        """
        if not isinstance(value, (list, tuple, set)):
            return None
        values = list(value)
        non_null_values = [element for element in values if element is not None]
        if not non_null_values:
            return None
        criterion = self.get_membership_criterion(term, values, non_null_values, element_type)
        if criterion is None:
            return None
        return criterion, len(non_null_values) != len(values)

    def is_in(self, term: Term, value: Any, element_type: str | None = None) -> Criterion:
        """``field__in=value``.

        Args:
            term: The compared term.
            value: The encoded lookup value.
            element_type: The compared term's SQL type, None to type by the values.

        Returns:
            The criterion.
        """
        membership = self.get_criterion(term, value, element_type)
        if membership is None:
            return Lookups.is_in(term, value)
        criterion, has_none = membership
        return criterion | term.isnull() if has_none else criterion

    def not_in(self, term: Term, value: Any, element_type: str | None = None) -> Criterion:
        """``field__not_in=value``.

        Args:
            term: The compared term.
            value: The encoded lookup value.
            element_type: The compared term's SQL type, None to type by the values.

        Returns:
            The criterion.
        """
        membership = self.get_criterion(term, value, element_type)
        if membership is None:
            return Lookups.not_in(term, value)
        criterion, has_none = membership
        negated_criterion: Criterion = Not(criterion)
        return negated_criterion if has_none else negated_criterion | term.isnull()

    def row_is_in(
        self, field: Tuple, value: list[tuple[Any, ...]], element_types: Sequence[str | None] | None = None
    ) -> Criterion:
        """Row-value ``__in``.

        Args:
            field: The compared columns.
            value: The encoded value rows.
            element_types: Per column, its SQL type, None to type it by its values.

        Returns:
            The criterion.
        """
        container = self.get_row_container(value, element_types) if value else None
        if container is None:
            return Lookups.row_is_in(field, value)
        return field.isin(container)

    def row_not_in(
        self, field: Tuple, value: list[tuple[Any, ...]], element_types: Sequence[str | None] | None = None
    ) -> Criterion:
        """Row-value ``__not_in``.

        Args:
            field: The compared columns.
            value: The encoded value rows.
            element_types: Per column, its SQL type, None to type it by its values.

        Returns:
            The criterion.
        """
        container = self.get_row_container(value, element_types) if value else None
        if container is None:
            return Lookups.row_not_in(field, value)
        return ~field.isin(container) | field.values[0].isnull()
