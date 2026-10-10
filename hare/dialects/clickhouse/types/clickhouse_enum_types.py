from __future__ import annotations

import zlib
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_ENUM8_RANGE,
    CLICKHOUSE_ENUM16_RANGE,
    CLICKHOUSE_ENUM_NUMBER_SPAN,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance
    from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseEnumTypes:
    """How ClickHouse stores enums - ``Enum8``/``Enum16`` of labels and their numbers. ClickHouse
    keeps the numbers and reads the labels off the type, so a label keeps its number for good: an
    ``IntEnumField``'s labels are its members' names, numbered by their values; a ``CharEnumField``'s
    are its stored values, numbered from the label itself - a member added, removed or moved changes
    no other label's number."""

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
    def get_char_enum_column_type(cls, field: Field[Any]) -> str:
        """The enum type of a ``CharEnumField`` - its stored values as labels.

        Args:
            field: The field.

        Returns:
            The type.
        """
        enum_field = cast("CharEnumFieldInstance", field)
        return cls.get_enum_type(cls.get_label_numbers([str(member.value) for member in enum_field.enum_type]))

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
