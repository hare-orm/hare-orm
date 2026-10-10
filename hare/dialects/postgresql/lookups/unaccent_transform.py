from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.dialects.postgresql.lookups.constants import UNACCENT_EXTENSION, UNACCENT_PATH_SEGMENT
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.filters.lookups.field_transforms import TermTransform


class UnaccentTransform:
    """``name__unaccent`` - a text value without its accents, through the ``unaccent`` extension's
    ``unaccent()``: ``name__unaccent__icontains="cafe"`` matches "Café"."""

    @staticmethod
    def get_transform(field: Field[Any]) -> tuple[TermTransform, Field[Any]]:
        """The transform of one text field - the value keeps the field's type.

        Args:
            field: The text field.

        Returns:
            The term transform and the field of its result.
        """
        return partial(Function, "unaccent"), field

    @classmethod
    def register(cls) -> None:
        """Adds the ``unaccent`` path segment to hare's text fields."""
        for field_class in (CharField, TextField, CitextField):
            field_class.register_transform(
                UNACCENT_PATH_SEGMENT, cls.get_transform, required_extension=UNACCENT_EXTENSION
            )
