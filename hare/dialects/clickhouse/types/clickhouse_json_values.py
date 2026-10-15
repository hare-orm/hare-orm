from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_ESCAPED_JSON_KEY_DOT,
    CLICKHOUSE_JSON_KEY_DOT,
    CLICKHOUSE_JSON_STRING_PATTERN,
)
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

    @classmethod
    def to_db_escaping_key_dots(cls, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The JSON text of an object, a dot of each key escaped as the server escapes it - a mutation
        reads the text with the server's own settings, without json_type_escape_dots_in_keys.

        Args:
            field: The JSON field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The text, None for None.

        Raises:
            ValidationError: The value isn't an object, or a key holds the escape itself, which the
                server would read back as a dot.
        """
        text = cls.to_db(field, value, instance)
        if text is None or (CLICKHOUSE_JSON_KEY_DOT not in text and CLICKHOUSE_ESCAPED_JSON_KEY_DOT not in text):
            return text

        def escape_key(match: re.Match[str]) -> str:
            string = match.group()
            if match.group("colon") is None:
                return string
            if CLICKHOUSE_ESCAPED_JSON_KEY_DOT in string:
                raise ValidationError(
                    f"{field.model_field_name}: a key of a JSON column of ClickHouse can't hold "
                    f"{CLICKHOUSE_ESCAPED_JSON_KEY_DOT!r} - the server reads it back as a dot"
                )
            return string.replace(CLICKHOUSE_JSON_KEY_DOT, CLICKHOUSE_ESCAPED_JSON_KEY_DOT)

        return CLICKHOUSE_JSON_STRING_PATTERN.sub(escape_key, text)

    @classmethod
    def to_db_refusing_key_dots(cls, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The JSON text of an object, refused when a key holds a dot - a server before ClickHouse 25.8
        stores it as a nested path.

        Args:
            field: The JSON field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The text, None for None.

        Raises:
            ValidationError: The value isn't an object, or a key holds a dot.
        """
        text = cls.to_db(field, value, instance)
        if text is None or CLICKHOUSE_JSON_KEY_DOT not in text:
            return text
        for match in CLICKHOUSE_JSON_STRING_PATTERN.finditer(text):
            if match.group("colon") is not None and CLICKHOUSE_JSON_KEY_DOT in match.group():
                raise ValidationError(
                    f"{field.model_field_name}: a key of a JSON column of ClickHouse before 25.8 can't hold a dot "
                    f"- the server stores {match.group()} as a nested path"
                )
        return text
