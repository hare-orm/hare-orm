from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ValidationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.json.json_field import JSONField
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseJsonValues:
    """How ClickHouse writes a JSONField's value into a ``JSON`` column - its JSON text, which must be
    an object: the type holds an object of paths, no array, string or number at its top."""

    @staticmethod
    def to_db(field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The JSON text of an object.

        Args:
            field: The JSON field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The text, None for None.

        Raises:
            ValidationError: The value isn't an object.
        """
        text = cast("JSONField[Any]", field).to_db_value(value, instance)  # type: ignore[arg-type]
        if text is None:
            return None
        if not text.lstrip().startswith("{"):
            raise ValidationError(
                f"{field.model_field_name}: a JSON column of ClickHouse holds an object, got "
                f"{field.get_value_for_message(value)}"
            )
        return text
