from __future__ import annotations

from functools import partial
from typing import Any

from hare.exceptions import (
    FieldError,
)
from hare.query.filters.lookups.constants import ANNOTATION_CONTAINER_LOOKUPS
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class AnnotationContainerLookups:
    """The array/range/JSON-only lookups of an annotation whose value type isn't known yet."""

    @staticmethod
    def reject(lookup_name: str, term: Term, value: Any) -> Criterion:
        """Backs an array/range/JSON-only lookup on an annotation whose value is none of those.

        Raises:
            FieldError: Always.
        """
        raise FieldError(
            f"__{lookup_name} needs an annotation whose value is an array, range or JSON value "
            "(e.g. ArrayAgg(...), F('array_field'))"
        )

    @classmethod
    def get_lookups(cls) -> dict[str, FieldLookup]:
        """The array/range/JSON-only lookups of a value with no field, each rejecting.

        Returns:
            The lookups by suffix.
        """
        return {
            lookup_name: FieldLookup(partial(cls.reject, lookup_name))
            for lookup_name in sorted(ANNOTATION_CONTAINER_LOOKUPS)
        }
