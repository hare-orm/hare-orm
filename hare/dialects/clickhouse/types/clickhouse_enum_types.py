from __future__ import annotations

import enum
import zlib
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_ENUM8_RANGE,
    CLICKHOUSE_ENUM16_RANGE,
    CLICKHOUSE_ENUM_DRIVER_LABELS_CACHE_SIZE,
    CLICKHOUSE_ENUM_NUMBER_SPAN,
    CLICKHOUSE_STRING_ESCAPE_PATTERN,
    CLICKHOUSE_TYPE_TEXT_ESCAPES,
)
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance
    from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
    from hare.fields.field import Field
    from hare.models import Model
    from hare.sql.terms.term import Term


class ClickhouseEnumTypes:
    """How ClickHouse stores enums - ``Enum8``/``Enum16`` of labels and their numbers. ClickHouse
    keeps the numbers and reads the labels off the type, so a label keeps its number for good: an
    ``IntEnumField``'s labels are its members' names, numbered by their values; a ``CharEnumField``'s
    are its stored values, numbered from the label itself - a member added, removed or moved changes
    no other label's number."""

    #: (enum class,) -> the stored values by the labels the drivers read otherwise.
    DRIVER_LABELS: ClassVar[Cache[dict[str, str]]] = Cache(CLICKHOUSE_ENUM_DRIVER_LABELS_CACHE_SIZE)
    #: (enum class,) -> the number of each stored value of a CharEnumField's enum.
    LABEL_NUMBERS: ClassVar[Cache[dict[str, int]]] = Cache(CLICKHOUSE_ENUM_DRIVER_LABELS_CACHE_SIZE)

    @staticmethod
    def get_label_numbers(labels: list[str]) -> dict[str, int]:
        """The number of each label of a ``CharEnumField`` - from a checksum of the label, the next free
        one after a taken one, the labels taken in their order.

        Args:
            labels: The labels.

        Returns:
            The numbers by label.
        """
        lowest, _highest = CLICKHOUSE_ENUM16_RANGE
        numbers: dict[str, int] = {}
        taken: set[int] = set()
        for label in sorted(labels):
            offset = zlib.crc32(label.encode("utf-8")) % CLICKHOUSE_ENUM_NUMBER_SPAN
            while offset in taken:
                offset = (offset + 1) % CLICKHOUSE_ENUM_NUMBER_SPAN
            taken.add(offset)
            numbers[label] = lowest + offset
        return numbers

    @staticmethod
    def get_enum_type(numbers_by_label: dict[str, int]) -> str:
        """``Enum8(...)`` when every number fits it, else ``Enum16(...)``.

        Args:
            numbers_by_label: The number of each label.

        Returns:
            The type.
        """
        # Local import: the dialect constants import the types module.
        from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

        lowest, highest = CLICKHOUSE_ENUM8_RANGE
        type_name = "Enum8" if all(lowest <= number <= highest for number in numbers_by_label.values()) else "Enum16"
        members_sql = ", ".join(
            f"{CLICKHOUSE_DIALECT.literals.get_literal_sql(label)} = {number}"
            for label, number in sorted(numbers_by_label.items(), key=lambda item: item[1])
        )
        return f"{type_name}({members_sql})"

    @classmethod
    def get_char_enum_numbers(cls, enum_type: type[enum.Enum]) -> dict[str, int]:
        """The number of each stored value of a ``CharEnumField``'s enum, kept.

        Args:
            enum_type: The enum.

        Returns:
            The numbers by stored value.
        """
        numbers = cls.LABEL_NUMBERS.get((enum_type,))
        if numbers is None:
            numbers = cls.LABEL_NUMBERS[(enum_type,)] = cls.get_label_numbers(
                [str(member.value) for member in enum_type]
            )
        return numbers

    @classmethod
    def get_char_enum_db_value(cls, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """A ``CharEnumField``'s written value as the number of its label - a driver writes a label by
        the labels it read off the text of the type, a backslash, a new line or a tab of one still
        escaped there.

        Args:
            field: The field.
            value: The Python value.
            instance: The model (class) it is written for.

        Returns:
            The number, None for None.
        """
        enum_field = cast("CharEnumFieldInstance", field)
        stored_value = enum_field.to_db_value(value, instance)  # type: ignore[arg-type]
        if stored_value is None:
            return None
        return cls.get_char_enum_numbers(enum_field.enum_type)[str(stored_value)]

    @classmethod
    def get_char_enum_column_type(cls, field: Field[Any]) -> str:
        """The enum type of a ``CharEnumField`` - its stored values as labels.

        Args:
            field: The field.

        Returns:
            The type.
        """
        enum_field = cast("CharEnumFieldInstance", field)
        return cls.get_enum_type(cls.get_char_enum_numbers(enum_field.enum_type))

    @classmethod
    def get_int_enum_column_type(cls, field: Field[Any]) -> str:
        """The enum type of an ``IntEnumField`` - its members' names numbered by their values.

        Args:
            field: The field.

        Returns:
            The type.
        """
        enum_field = cast("IntEnumFieldInstance", field)
        return cls.get_enum_type({member.name: int(member.value) for member in enum_field.enum_type})

    @staticmethod
    def get_char_enum_text(field: Field[Any], term: Term) -> Term:
        """A ``CharEnumField``'s column as its label - an ``Enum8``/``Enum16`` compares and sorts by
        the number its label has, a checksum of the label, where PostgreSQL compares the text.

        Args:
            field: The field.
            term: The column.

        Returns:
            The label.
        """
        return Function("toString", term)

    @staticmethod
    def get_char_enum_python_value(field: Field[Any], value: Any) -> Any:
        """A ``CharEnumField``'s member of the label the driver returned - a driver reads the labels off
        the text of the type, a backslash, a new line or a tab of one still escaped there.

        Args:
            field: The field.
            value: The label.

        Returns:
            The member, None for None.
        """
        if isinstance(value, str):
            value = ClickhouseEnumTypes.get_driver_labels(cast("CharEnumFieldInstance", field).enum_type).get(
                value, value
            )
        return field.from_db_value(value)

    @classmethod
    def get_driver_labels(cls, enum_type: type[enum.Enum]) -> dict[str, str]:
        """The stored value of each label a driver reads otherwise - clickhouse-connect takes the
        label as the text of the type escapes it, clickhouse-driver drops each escaping backslash.

        Args:
            enum_type: The enum of a ``CharEnumField``.

        Returns:
            The stored values by the labels the drivers read, the ones they read right left out.
        """
        labels = cls.DRIVER_LABELS.get((enum_type,))
        if labels is None:
            labels = {}
            for member in enum_type:
                stored_value = str(member.value)
                escaped = "".join(CLICKHOUSE_TYPE_TEXT_ESCAPES.get(character, character) for character in stored_value)
                for driver_label in (escaped, CLICKHOUSE_STRING_ESCAPE_PATTERN.sub(r"\1", escaped)):
                    if driver_label != stored_value:
                        labels.setdefault(driver_label, stored_value)
            cls.DRIVER_LABELS[(enum_type,)] = labels
        return labels

    @staticmethod
    def get_int_enum_db_value(field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """An ``IntEnumField``'s value as its label - the member's name.

        Args:
            field: The field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The label, None for None.
        """
        enum_field = cast("IntEnumFieldInstance", field)
        number = enum_field.to_db_value(value, instance)  # type: ignore[arg-type]
        return None if number is None else enum_field.enum_type(number).name

    @staticmethod
    def get_int_enum_python_value(field: Field[Any], value: Any) -> Any:
        """An ``IntEnumField``'s member of the label the driver returned.

        Args:
            field: The field.
            value: The label - or the number, for a value the server gave as one.

        Returns:
            The member, None for None.
        """
        enum_field = cast("IntEnumFieldInstance", field)
        if value is None:
            return None
        if isinstance(value, str):
            return enum_field.enum_type[value]
        return enum_field.to_python(value)
