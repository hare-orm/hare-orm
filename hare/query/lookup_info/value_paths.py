from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.encrypted.encrypted_json_field import EncryptedJSONField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.filters.constants import (
    CALENDAR_DATE_PART_LOOKUPS,
    DATETIME_CAST_SEGMENTS,
    DATETIME_DATE_PART_LOOKUPS,
    TIME_OF_DAY_DATE_PART_LOOKUPS,
)
from hare.query.filters.lookups.field_transforms import FieldTransforms
from hare.query.filters.lookups.json.json_filter_parser import JsonFilterParser
from hare.query.lookup_info.lookup_path import LookupPath
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class ValuePaths:
    """Names reading inside a field's value: a JSON key path or an array/range transform, and a part of
    a date, time or datetime - split into the field and the path, and the term reading it."""

    @staticmethod
    def get_value_path_split(model: type[Model], name: str) -> tuple[str, Field[Any], list[str]] | None:
        """Splits a name reading a path inside a field's value - a JSON key path
        (``data__owner``) or an array/range transform (``tags__0``, ``during__startswith``),
        after any relations (``crates__tags__0``).

        Args:
            model: The model the name starts at.
            name: The name.

        Returns:
            The field's own path, the field, and the path segments inside its value - or None
            when the name doesn't read inside a JSON, array or range field.
        """
        lookup_path = LookupPath.parse(model, name)
        if len(lookup_path.rest) < 2:
            return None
        field_name, *path_segments = lookup_path.rest
        field = lookup_path.model._meta.fields_map.get(field_name)
        if field is None:
            return None
        field_path = f"{lookup_path.prefix}{field_name}"
        if isinstance(field, JSONField):
            return field_path, field, path_segments
        transforms, __, rest_segments = FieldTransforms.get_path(field, path_segments)
        if transforms and not rest_segments:
            return field_path, field, path_segments
        return None

    @staticmethod
    def get_date_part_segments(field: Field[Any]) -> frozenset[str]:
        """The segments reading a part of a date, time or datetime field's value - a datetime's
        date parts, ``date`` and ``time``, a date's calendar parts, a time's time-of-day parts.

        Args:
            field: The field.

        Returns:
            The segments; empty for any other field.
        """
        effective_field = GeneratedField.get_effective_field(field)
        if isinstance(effective_field, DatetimeField):
            return DATETIME_CAST_SEGMENTS | frozenset(DATETIME_DATE_PART_LOOKUPS)
        if isinstance(effective_field, DateField):
            return frozenset(CALENDAR_DATE_PART_LOOKUPS)
        if isinstance(effective_field, TimeField):
            return frozenset(TIME_OF_DAY_DATE_PART_LOOKUPS)
        return frozenset()

    @staticmethod
    def get_date_part_split(model: type[Model], name: str) -> tuple[str, str] | None:
        """Splits a name reading a part of a date, time or datetime field - ``created__year``,
        ``created__date``, ``starts__hour`` - after any relations (``tournament__created__month``).

        Args:
            model: The model the name starts at.
            name: The name.

        Returns:
            The field's own path and the part, or None when the name reads no such part.
        """
        lookup_path = LookupPath.parse(model, name)
        if len(lookup_path.rest) != 2:
            return None
        field_name, part = lookup_path.rest
        field = lookup_path.model._meta.fields_map.get(field_name)
        if field is None or field_name in lookup_path.model._meta.fetch_fields:
            return None
        if part not in ValuePaths.get_date_part_segments(field):
            return None
        return f"{lookup_path.prefix}{field_name}", part

    @staticmethod
    def get_value_path_term(
        term: Term, field: Field[Any], path_segments: list[str], name: str
    ) -> tuple[Term, Field[Any]]:
        """The term reading a path inside a field's value.

        Args:
            term: The field's column term.
            field: The JSON, array or range field.
            path_segments: The path inside its value.
            name: The whole name, for the error message.

        Returns:
            The term and the field of the value it reads.

        Raises:
            FieldError: If the field is an encrypted JSON field.
        """
        if isinstance(field, JSONField):
            if isinstance(field, EncryptedJSONField):
                raise FieldError(
                    f"{field.get_field_label()} is encrypted - {name!r} would read a stored Fernet token, "
                    "not the key's value. Reference the whole field instead."
                )
            key_parts = JsonFilterParser.get_key_parts("__".join(path_segments))
            return JsonFilterParser.get_field_path(term, key_parts, as_text=False), field.get_path_value_field()
        transforms, output_field, __ = FieldTransforms.get_path(field, path_segments)
        return FieldTransforms.apply(transforms, term), output_field
