from __future__ import annotations

from typing import Any

from hare.sql.functions.coalesce import Coalesce
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class ArrayLength:
    """The length of an array's first dimension."""

    @staticmethod
    def get_term(term: Any) -> Term:
        """The number of rows of ``term`` - 0 for an empty array, NULL for NULL.

        Args:
            term: The array-valued expression.

        Returns:
            The length term.
        """
        # array_length(field, 1) counts the rows of a nested array (cardinality() would count every
        # element across all dimensions), but is NULL for an empty array - cardinality() fills that
        # in as 0 while staying NULL for a NULL column, which must match no __len value at all.
        return Coalesce(Function("array_length", term, 1), Function("cardinality", term))
