from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.fields.data.json.json_field import JSONField
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.query.filters.lookups.field_lookup import FieldLookup


class JSONPathField(JSONField[Any]):
    """The value at a JSON path of a ``JSONField`` - the output field of ``F("data__key")``.

    The value reads back as parsed JSON, and a filter on it compares JSON values in ``jsonb`` order
    on every backend (the number ``10`` and the string ``"10"`` differ, numbers order numerically).
    """

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The lookups of the value at a JSON path."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.lookups.json.json_path_lookups import JsonPathLookups

        return JsonPathLookups.get_lookups()

    def encode_comparison_value(self, value: Any, dialect: Dialect, *, as_parameter: bool = False) -> Any:
        """Encodes one filter value to compare with the JSON value at the path.

        Args:
            value: A JSON-compatible Python value; ``None`` is the JSON ``null``.
            dialect: The dialect of the database the query runs on.
            as_parameter: Return the bare JSON text, for a list bound as one array parameter.

        Returns:
            The value in the form the dialect compares it with the path's own expression.

        Raises:
            ValidationError: The value isn't JSON serializable or isn't a finite number.
        """
        if isinstance(value, Term):
            return value
        return dialect.renderers.get_json_path_comparand(
            value, self.encode_json_text, self.get_column_type(dialect), as_parameter=as_parameter
        )

    def encode_json_text(self, value: Any) -> str:
        """A compared value as JSON text.

        Args:
            value: A JSON-compatible Python value; ``None`` is the JSON ``null``.

        Returns:
            The JSON text.

        Raises:
            ValidationError: The value isn't JSON serializable or isn't a finite number.
        """
        if value is None:
            return "null"
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValidationError(
                    f"{self.model_field_name}: value {self.get_value_for_message(value)} is not a finite number"
                )
            return str(value)
        return self.encode_value(value)
