from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.lookups.declarations import ClickhouseStoredJsonText
from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_ESCAPED_JSON_KEY_DOT,
    CLICKHOUSE_JSON_COLUMN_TYPE,
    CLICKHOUSE_JSON_DIGITS_AS_ZEROS,
    CLICKHOUSE_JSON_EXACT_FLOAT_INTEGER,
    CLICKHOUSE_JSON_INTEGER_RANGE,
    CLICKHOUSE_JSON_KEY_DOT,
    CLICKHOUSE_JSON_LONG_INTEGER_MARK,
    CLICKHOUSE_JSON_NOT_KEPT_MARKS,
    CLICKHOUSE_JSON_STRING_PATTERN,
    CLICKHOUSE_JSON_TEXT_COLUMN_TYPE,
)
from hare.exceptions import ValidationError

if TYPE_CHECKING:
    from hare.fields.data.json.json_field import JSONField
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseJsonValues:
    """How ClickHouse writes a JSONField's value into a ``JSON`` column - its JSON text, which must be
    an object: the type holds an object of paths, no array, string or number at its top."""

    @staticmethod
    def get_column_type(field: Field[Any]) -> str:
        """A ``JSON`` column, or a ``String`` one of the JSON text for a field keeping its text
        (``JSONField.keeps_json_text``).

        Args:
            field: The JSON field.

        Returns:
            The column type.
        """
        return (
            CLICKHOUSE_JSON_TEXT_COLUMN_TYPE
            if cast("JSONField[Any]", field).keeps_json_text
            else CLICKHOUSE_JSON_COLUMN_TYPE
        )

    @staticmethod
    def to_lookup(field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The JSON text a filter compares a field keeping its text with (``JSONTextField``) - marked:
        it is compared as that text, not as a ``JSON`` value.

        Args:
            field: The JSON field.
            value: The Python value.
            instance: The model (class) it is compared for.

        Returns:
            The text, None for None.
        """
        text = cast("JSONField[Any]", field).to_lookup_value(value, instance)
        return text if text is None or not isinstance(text, str) else ClickhouseStoredJsonText(text)

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
            ValidationError: The value isn't an object, or holds what the column doesn't keep
                (``raise_if_not_kept()``).
        """
        text = cast("JSONField[Any]", field).to_db_value(value, instance)
        if text is None:
            return None
        if not text.lstrip().startswith("{"):
            raise ValidationError(
                f"{field.model_field_name}: a JSON column of ClickHouse holds an object, got "
                f"{field.get_value_for_message(value)}"
            )
        if ClickhouseJsonValues.may_hold_not_kept_values(text):
            ClickhouseJsonValues.raise_if_not_kept(field, value if isinstance(value, dict) else json.loads(text))
        return text

    @staticmethod
    def may_hold_not_kept_values(text: str) -> bool:
        """Whether a JSON text may hold a value the column doesn't keep - read off the text, much
        faster than a walk of the value.

        Args:
            text: The JSON text.

        Returns:
            False for a text holding no null, no empty object and no integer of 16 digits.
        """
        return any(mark in text for mark in CLICKHOUSE_JSON_NOT_KEPT_MARKS) or (
            CLICKHOUSE_JSON_LONG_INTEGER_MARK in text.translate(CLICKHOUSE_JSON_DIGITS_AS_ZEROS)
        )

    @staticmethod
    def raise_if_not_kept(field: Field[Any], value: dict[str, Any]) -> None:
        """Refuses an object the column would read back otherwise - it keeps no path to a null or an
        empty object, an integer past an Int64/UInt64 only as a float, and the numbers of one array
        (its nested arrays included) as one type: floats, when one of them is.

        Args:
            field: The JSON field.
            value: The object.

        Raises:
            ValidationError: A key holds None or an empty object, an integer is out of range, or an
                array holds a float beside an integer a float doesn't hold exactly.
        """
        minimum, maximum = CLICKHOUSE_JSON_INTEGER_RANGE
        name = field.model_field_name
        pending: list[Any] = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, member in item.items():
                    if member is None or (isinstance(member, dict) and not member):
                        raise ValidationError(
                            f"{name}: a JSON column of ClickHouse keeps no key holding {member!r} - {key!r} would "
                            "be read back missing"
                        )
                    pending.append(member)
            elif isinstance(item, list | tuple):
                # The numbers of one array and of the arrays in it - the objects in it keep paths of
                # their own.
                array_items: list[Any] = [item]
                holds_float = False
                widest_integer = 0
                while array_items:
                    for element in array_items.pop():
                        if isinstance(element, list | tuple):
                            array_items.append(element)
                        elif type(element) is float:
                            holds_float = True
                        elif type(element) is int:
                            if not minimum <= element <= maximum:
                                raise ValidationError(
                                    f"{name}: a JSON column of ClickHouse keeps an integer from {minimum} to "
                                    f"{maximum} - another one is read back as a float"
                                )
                            widest_integer = max(widest_integer, abs(element))
                        else:
                            pending.append(element)
                if holds_float and widest_integer > CLICKHOUSE_JSON_EXACT_FLOAT_INTEGER:
                    raise ValidationError(
                        f"{name}: a JSON column of ClickHouse reads an array holding a float as floats - an "
                        f"integer past {CLICKHOUSE_JSON_EXACT_FLOAT_INTEGER} beside one would lose digits"
                    )
            elif type(item) is int and not minimum <= item <= maximum:
                raise ValidationError(
                    f"{name}: a JSON column of ClickHouse keeps an integer from {minimum} to {maximum} - another "
                    "one is read back as a float"
                )

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
        if cast("JSONField[Any]", field).keeps_json_text:
            # Its JSON text, as it is.
            return cast("JSONField[Any]", field).to_db_value(value, instance)
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
