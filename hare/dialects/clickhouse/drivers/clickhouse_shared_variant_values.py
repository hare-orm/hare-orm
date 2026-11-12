from __future__ import annotations

import datetime
import ipaddress
import struct
import uuid
import zoneinfo
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_BINARY_DATETIME64_TAG,
    CLICKHOUSE_BINARY_DATETIME_TAG,
    CLICKHOUSE_BINARY_DECIMAL_TAGS,
    CLICKHOUSE_BINARY_DECIMAL_WIDTHS,
    CLICKHOUSE_BINARY_ENUM8_TAG,
    CLICKHOUSE_BINARY_ENUM16_TAG,
    CLICKHOUSE_BINARY_EPOCH_DATE,
    CLICKHOUSE_BINARY_EPOCH_MOMENT,
    CLICKHOUSE_BINARY_FIXED_STRING_TAG,
    CLICKHOUSE_BINARY_INTEGER_WIDTHS,
    CLICKHOUSE_BINARY_MAP_TAG,
    CLICKHOUSE_BINARY_NAMED_TUPLE_TAG,
    CLICKHOUSE_BINARY_PLAIN_TYPE_TAGS,
    CLICKHOUSE_BINARY_PLAIN_TYPES_BY_TAG,
    CLICKHOUSE_BINARY_TUPLE_TAG,
    CLICKHOUSE_BINARY_TYPE_CACHE_MAX_SIZE,
    CLICKHOUSE_BINARY_WRAPPER_TAGS,
    CLICKHOUSE_BINARY_WRAPPERS_BY_TAG,
    CLICKHOUSE_BINARY_ZONED_DATETIME64_TAG,
    CLICKHOUSE_BINARY_ZONED_DATETIME_TAG,
)
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType
from hare.exceptions import UnSupportedError


class ClickhouseSharedVariantValues:
    """A value of a ``Dynamic`` column's shared variant in ClickHouse's binary form - the binary
    encoding of its type followed by the value as the type writes one: what both drivers write
    a ``Dynamic`` value as (every value of a column in the shared variant, so the column's structure
    needs no reading of its values), and how they read one the server keeps there - the drivers'
    own reading leaves out decimals, dates and the containers of them."""

    #: The binary encoding of each type written, by its text.
    type_encodings: ClassVar[Cache[bytes]] = Cache(CLICKHOUSE_BINARY_TYPE_CACHE_MAX_SIZE, holds_sql=False)

    @staticmethod
    def get_decimal_width(precision: int) -> tuple[int, int]:
        """The tag and the bytes of the narrowest decimal holding ``precision`` digits.

        Args:
            precision: The digits.

        Returns:
            The tag and the bytes.
        """
        for (most_digits, width), tag in zip(
            CLICKHOUSE_BINARY_DECIMAL_WIDTHS, CLICKHOUSE_BINARY_DECIMAL_TAGS, strict=True
        ):
            if precision <= most_digits:
                return tag, width
        raise UnSupportedError(f"A decimal of {precision} digits has no ClickHouse type")

    @staticmethod
    def write_varint(number: int, blob: bytearray) -> None:
        """Writes an unsigned number in LEB128.

        Args:
            number: The number.
            blob: The bytes written into.
        """
        while True:
            byte = number & 0x7F
            number >>= 7
            if number:
                blob.append(byte | 0x80)
            else:
                blob.append(byte)
                return

    @classmethod
    def write_bytes(cls, data: bytes, blob: bytearray) -> None:
        """Writes bytes after their length.

        Args:
            data: The bytes.
            blob: The bytes written into.
        """
        cls.write_varint(len(data), blob)
        blob += data

    @classmethod
    def get_blob(cls, value: Any, type_text: str) -> bytes:
        """A value of the shared variant - its type's binary encoding, then the value.

        Args:
            value: The value, as a driver's binary insert takes one of the type.
            type_text: Its type, as the server names it.

        Returns:
            The bytes.
        """
        plain_tag = CLICKHOUSE_BINARY_PLAIN_TYPE_TAGS.get(type_text)
        if plain_tag is not None:
            blob = bytearray((plain_tag,))
        else:
            key = (type_text,)
            type_encoding: bytes | None = cls.type_encodings.get(key)
            if type_encoding is None:
                encoding = bytearray()
                cls.write_type(type_text, encoding)
                type_encoding = cls.type_encodings[key] = bytes(encoding)
            blob = bytearray(type_encoding)
        cls.write_value(value, type_text, blob)
        return bytes(blob)

    @classmethod
    def write_type(cls, type_text: str, blob: bytearray) -> None:
        """Writes a type's binary encoding.

        Args:
            type_text: The type.
            blob: The bytes written into.

        Raises:
            UnSupportedError: The type has no binary encoding here.
        """
        physical_type = ClickhousePhysicalType.from_sql(type_text)
        name, arguments = physical_type.name, physical_type.arguments
        plain_tag = CLICKHOUSE_BINARY_PLAIN_TYPE_TAGS.get(name)
        if plain_tag is not None and not arguments:
            blob.append(plain_tag)
            return
        writer = cls.TYPE_WRITERS.get(name)
        if writer is None:
            raise UnSupportedError(f"A value of type {type_text} isn't written into a Dynamic column")
        writer(name, arguments, blob)

    @staticmethod
    def write_datetime_type(name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes the encoding of ``DateTime`` or ``DateTime64`` - with its time zone when it has one.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        zone_position = 0 if name == ClickhouseTypeName.DATETIME else 1
        is_zoned = len(arguments) > zone_position
        if name == ClickhouseTypeName.DATETIME:
            blob.append(CLICKHOUSE_BINARY_ZONED_DATETIME_TAG if is_zoned else CLICKHOUSE_BINARY_DATETIME_TAG)
        else:
            blob.append(CLICKHOUSE_BINARY_ZONED_DATETIME64_TAG if is_zoned else CLICKHOUSE_BINARY_DATETIME64_TAG)
            blob.append(int(arguments[0]))
        if is_zoned:
            zone_name = ClickhouseSqlParts.get_string_text(arguments[zone_position].strip())
            ClickhouseSharedVariantValues.write_bytes(zone_name.encode(), blob)

    @staticmethod
    def write_sized_type(name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes the encoding of ``Decimal`` or ``FixedString`` - with its size.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        if name == ClickhouseTypeName.FIXED_STRING:
            blob.append(CLICKHOUSE_BINARY_FIXED_STRING_TAG)
            ClickhouseSharedVariantValues.write_varint(int(arguments[0]), blob)
            return
        precision, scale = int(arguments[0]), int(arguments[1])
        blob.append(ClickhouseSharedVariantValues.get_decimal_width(precision)[0])
        blob += bytes((precision, scale))

    @staticmethod
    def write_wrapper_type(name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes the encoding of ``Array``, ``Nullable``, ``LowCardinality`` or ``Map`` - with the
        types inside.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        if name == ClickhouseTypeName.MAP:
            blob.append(CLICKHOUSE_BINARY_MAP_TAG)
            ClickhouseSharedVariantValues.write_type(arguments[0], blob)
            ClickhouseSharedVariantValues.write_type(arguments[1], blob)
            return
        blob.append(CLICKHOUSE_BINARY_WRAPPER_TAGS[name])
        ClickhouseSharedVariantValues.write_type(arguments[0], blob)

    @staticmethod
    def write_tuple_type(name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes the encoding of ``Tuple`` - with its elements' types, and their names when every
        element has one.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        elements = [ClickhousePhysicalType.get_element(argument) for argument in arguments]
        is_named = bool(elements) and all(element_name is not None for element_name, _ in elements)
        blob.append(CLICKHOUSE_BINARY_NAMED_TUPLE_TAG if is_named else CLICKHOUSE_BINARY_TUPLE_TAG)
        ClickhouseSharedVariantValues.write_varint(len(elements), blob)
        for element_name, element_type in elements:
            if is_named and element_name is not None:
                ClickhouseSharedVariantValues.write_bytes(element_name.encode(), blob)
            ClickhouseSharedVariantValues.write_type(element_type.sql, blob)

    @staticmethod
    def write_enum_type(name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes the encoding of ``Enum8`` or ``Enum16`` - with its members.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        blob.append(CLICKHOUSE_BINARY_ENUM8_TAG if name == ClickhouseTypeName.ENUM8 else CLICKHOUSE_BINARY_ENUM16_TAG)
        members = ClickhousePhysicalType.get_enum_members(arguments)
        ClickhouseSharedVariantValues.write_varint(len(members), blob)
        for label, number in members.items():
            ClickhouseSharedVariantValues.write_bytes(label.encode(), blob)
            blob += number.to_bytes(1 if name == ClickhouseTypeName.ENUM8 else 2, "little", signed=True)

    @classmethod
    def write_value(cls, value: Any, type_text: str, blob: bytearray) -> None:
        """Writes a value as its type writes one.

        Args:
            value: The value.
            type_text: Its type.
            blob: The bytes written into.

        Raises:
            UnSupportedError: A value of the type isn't written here.
        """
        # A type without arguments is found by its text - nothing to parse.
        writer = cls.VALUE_WRITERS.get(type_text)
        if writer is not None:
            writer(value, type_text, (), blob)
            return
        physical_type = ClickhousePhysicalType.from_sql(type_text)
        name, arguments = physical_type.name, physical_type.arguments
        writer = cls.VALUE_WRITERS.get(name)
        if writer is None:
            raise UnSupportedError(f"A value of type {type_text} isn't written into a Dynamic column")
        writer(value, name, arguments, blob)

    @staticmethod
    def write_wrapped_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes a value of ``Nullable``, ``LowCardinality`` or ``Nothing``.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        if name == ClickhouseTypeName.NOTHING:
            return
        if name == ClickhouseTypeName.NULLABLE:
            blob.append(1 if value is None else 0)
            if value is None:
                return
        ClickhouseSharedVariantValues.write_value(value, arguments[0], blob)

    @staticmethod
    def write_number_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes an integer, a boolean or a float.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        if name == ClickhouseTypeName.BOOL:
            blob.append(1 if value else 0)
        elif name == ClickhouseTypeName.FLOAT32:
            blob += struct.pack("<f", value)
        elif name == ClickhouseTypeName.FLOAT64:
            blob += struct.pack("<d", value)
        else:
            width, signed = CLICKHOUSE_BINARY_INTEGER_WIDTHS[name]
            blob += int(value).to_bytes(width, "little", signed=signed)

    @staticmethod
    def write_text_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes a value of ``String`` or ``FixedString``.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        data = value.encode() if isinstance(value, str) else bytes(value)
        if name == ClickhouseTypeName.STRING:
            ClickhouseSharedVariantValues.write_bytes(data, blob)
        else:
            blob += data.ljust(int(arguments[0]), b"\x00")

    @staticmethod
    def write_temporal_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes a date or a moment.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        if name in {ClickhouseTypeName.DATE, ClickhouseTypeName.DATE32}:
            days = (value - CLICKHOUSE_BINARY_EPOCH_DATE).days
            blob += days.to_bytes(
                2 if name == ClickhouseTypeName.DATE else 4, "little", signed=name == ClickhouseTypeName.DATE32
            )
            return
        moment = value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
        elapsed = moment - CLICKHOUSE_BINARY_EPOCH_MOMENT
        microseconds = (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
        if name == ClickhouseTypeName.DATETIME:
            blob += (microseconds // 1_000_000).to_bytes(4, "little")
        else:
            ticks = microseconds * 10 ** int(arguments[0]) // 1_000_000
            blob += ticks.to_bytes(8, "little", signed=True)

    @staticmethod
    def write_decimal_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes a decimal.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        precision, scale = int(arguments[0]), int(arguments[1])
        parts = Decimal(value).as_tuple()
        unscaled = int(Decimal((parts.sign, parts.digits, parts.exponent + scale)))  # type: ignore[operator]
        width = ClickhouseSharedVariantValues.get_decimal_width(precision)[1]
        blob += unscaled.to_bytes(width, "little", signed=True)

    @staticmethod
    def write_identifier_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes a UUID or an IP address.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        if name == ClickhouseTypeName.UUID:
            number = uuid.UUID(str(value)).int
            blob += (number >> 64).to_bytes(8, "little") + (number & (2**64 - 1)).to_bytes(8, "little")
        elif name == ClickhouseTypeName.IPV4:
            blob += int(ipaddress.IPv4Address(value)).to_bytes(4, "little")
        else:
            address = ipaddress.ip_address(value)
            if isinstance(address, ipaddress.IPv4Address):
                address = ipaddress.IPv6Address(f"::ffff:{address}")
            blob += address.packed

    @staticmethod
    def write_container_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes an array, a tuple or a map.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        write_value = ClickhouseSharedVariantValues.write_value
        if name == ClickhouseTypeName.ARRAY:
            ClickhouseSharedVariantValues.write_varint(len(value), blob)
            for element in value:
                write_value(element, arguments[0], blob)
        elif name == ClickhouseTypeName.TUPLE:
            for element, argument in zip(value, arguments, strict=True):
                write_value(element, ClickhousePhysicalType.get_element(argument)[1].sql, blob)
        else:
            ClickhouseSharedVariantValues.write_varint(len(value), blob)
            for key, item in value.items():
                write_value(key, arguments[0], blob)
                write_value(item, arguments[1], blob)

    @staticmethod
    def write_enum_value(value: Any, name: str, arguments: Sequence[str], blob: bytearray) -> None:
        """Writes an enum's member - given as its label or its number.

        Args:
            value: The value.
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes written into.
        """
        number = ClickhousePhysicalType.get_enum_members(arguments)[value] if isinstance(value, str) else int(value)
        blob += number.to_bytes(1 if name == ClickhouseTypeName.ENUM8 else 2, "little", signed=True)

    @staticmethod
    def read_varint(blob: bytes, position: int) -> tuple[int, int]:
        """Reads an unsigned number in LEB128.

        Args:
            blob: The bytes.
            position: Where the number starts.

        Returns:
            The number, and where the bytes after it start.
        """
        number = 0
        shift = 0
        while True:
            byte = blob[position]
            position += 1
            number |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return number, position
            shift += 7

    @classmethod
    def read_bytes(cls, blob: bytes, position: int) -> tuple[bytes, int]:
        """Reads bytes written after their length.

        Args:
            blob: The bytes.
            position: Where the length starts.

        Returns:
            The bytes, and where the bytes after them start.
        """
        length, position = cls.read_varint(blob, position)
        return bytes(blob[position : position + length]), position + length

    @classmethod
    def decode(cls, blob: bytes) -> Any:
        """The Python value of a value of the shared variant.

        Args:
            blob: Its bytes - its type's binary encoding, then the value.

        Returns:
            The value - as the drivers read a value of its type, a moment with its time zone.
        """
        type_text, position = cls.read_type(bytes(blob), 0)
        return cls.read_value(type_text, bytes(blob), position)[0]

    @classmethod
    def read_type(cls, blob: bytes, position: int) -> tuple[str, int]:
        """Reads a type's binary encoding.

        Args:
            blob: The bytes.
            position: Where the type starts.

        Returns:
            The type, as the server names it, and where the bytes after it start.

        Raises:
            UnSupportedError: The type isn't one read here.
        """
        tag = blob[position]
        position += 1
        plain_type = CLICKHOUSE_BINARY_PLAIN_TYPES_BY_TAG.get(tag)
        if plain_type is not None:
            return plain_type, position
        if tag == CLICKHOUSE_BINARY_DATETIME_TAG:
            return ClickhouseTypeName.DATETIME, position
        if tag == CLICKHOUSE_BINARY_ZONED_DATETIME_TAG:
            zone, position = cls.read_bytes(blob, position)
            return f"DateTime('{zone.decode()}')", position
        if tag in {CLICKHOUSE_BINARY_DATETIME64_TAG, CLICKHOUSE_BINARY_ZONED_DATETIME64_TAG}:
            digits = blob[position]
            position += 1
            if tag == CLICKHOUSE_BINARY_DATETIME64_TAG:
                return f"DateTime64({digits})", position
            zone, position = cls.read_bytes(blob, position)
            return f"DateTime64({digits}, '{zone.decode()}')", position
        if tag in CLICKHOUSE_BINARY_DECIMAL_TAGS:
            return f"Decimal({blob[position]}, {blob[position + 1]})", position + 2
        if tag == CLICKHOUSE_BINARY_FIXED_STRING_TAG:
            length, position = cls.read_varint(blob, position)
            return f"FixedString({length})", position
        if tag in CLICKHOUSE_BINARY_WRAPPERS_BY_TAG:
            inner_type, position = cls.read_type(blob, position)
            return f"{CLICKHOUSE_BINARY_WRAPPERS_BY_TAG[tag]}({inner_type})", position
        if tag == CLICKHOUSE_BINARY_MAP_TAG:
            key_type, position = cls.read_type(blob, position)
            value_type, position = cls.read_type(blob, position)
            return f"Map({key_type}, {value_type})", position
        if tag in {CLICKHOUSE_BINARY_TUPLE_TAG, CLICKHOUSE_BINARY_NAMED_TUPLE_TAG}:
            return cls.read_tuple_type(blob, position, is_named=tag == CLICKHOUSE_BINARY_NAMED_TUPLE_TAG)
        if tag in {CLICKHOUSE_BINARY_ENUM8_TAG, CLICKHOUSE_BINARY_ENUM16_TAG}:
            return cls.read_enum_type(blob, position, 1 if tag == CLICKHOUSE_BINARY_ENUM8_TAG else 2)
        raise UnSupportedError(f"A Dynamic value of the type of binary tag 0x{tag:02x} isn't read")

    @classmethod
    def read_tuple_type(cls, blob: bytes, position: int, *, is_named: bool) -> tuple[str, int]:
        """Reads the rest of a tuple's encoding - its elements.

        Args:
            blob: The bytes.
            position: Where the count of the elements starts.
            is_named: Whether each element has a name.

        Returns:
            The type, and where the bytes after it start.
        """
        from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

        count, position = cls.read_varint(blob, position)
        element_types = []
        for _ in range(count):
            element_name = None
            if is_named:
                element_name, position = cls.read_bytes(blob, position)
            element_type, position = cls.read_type(blob, position)
            element_types.append(
                element_type
                if element_name is None
                else f"{CLICKHOUSE_DIALECT.literals.quote_identifier(element_name.decode())} {element_type}"
            )
        return f"Tuple({', '.join(element_types)})", position

    @classmethod
    def read_enum_type(cls, blob: bytes, position: int, width: int) -> tuple[str, int]:
        """Reads the rest of an enum's encoding - its members.

        Args:
            blob: The bytes.
            position: Where the count of the members starts.
            width: The bytes of a member's number.

        Returns:
            The type, and where the bytes after it start.
        """
        count, position = cls.read_varint(blob, position)
        members = []
        for _ in range(count):
            label, position = cls.read_bytes(blob, position)
            number = int.from_bytes(blob[position : position + width], "little", signed=True)
            position += width
            escaped_label = label.decode().replace("\\", "\\\\").replace("'", "\\'")
            members.append(f"'{escaped_label}' = {number}")
        return f"Enum{8 * width}({', '.join(members)})", position

    @classmethod
    def read_value(cls, type_text: str, blob: bytes, position: int) -> tuple[Any, int]:
        """Reads a value as its type writes one.

        Args:
            type_text: Its type.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.

        Raises:
            UnSupportedError: A value of the type isn't read here.
        """
        # A type without arguments is found by its text - nothing to parse.
        reader = cls.VALUE_READERS.get(type_text)
        if reader is not None:
            return reader(type_text, (), blob, position)
        physical_type = ClickhousePhysicalType.from_sql(type_text)
        name, arguments = physical_type.name, physical_type.arguments
        reader = cls.VALUE_READERS.get(name)
        if reader is None:
            raise UnSupportedError(f"A Dynamic value of type {type_text} isn't read")
        return reader(name, arguments, blob, position)

    @staticmethod
    def read_wrapped_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads a value of ``Nullable``, ``LowCardinality`` or ``Nothing``.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        if name == ClickhouseTypeName.NOTHING:
            return None, position
        if name == ClickhouseTypeName.NULLABLE:
            if blob[position]:
                return None, position + 1
            position += 1
        return ClickhouseSharedVariantValues.read_value(arguments[0], blob, position)

    @staticmethod
    def read_number_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads an integer, a boolean or a float.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        if name == ClickhouseTypeName.BOOL:
            return bool(blob[position]), position + 1
        if name == ClickhouseTypeName.FLOAT32:
            return struct.unpack_from("<f", blob, position)[0], position + 4
        if name == ClickhouseTypeName.FLOAT64:
            return struct.unpack_from("<d", blob, position)[0], position + 8
        width, signed = CLICKHOUSE_BINARY_INTEGER_WIDTHS[name]
        return int.from_bytes(blob[position : position + width], "little", signed=signed), position + width

    @staticmethod
    def read_text_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads a value of ``String`` or ``FixedString`` - as bytes when it isn't a text.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        if name == ClickhouseTypeName.STRING:
            data, position = ClickhouseSharedVariantValues.read_bytes(blob, position)
        else:
            length = int(arguments[0])
            data, position = blob[position : position + length], position + length
        try:
            return data.decode(), position
        except UnicodeDecodeError:
            return data, position

    @staticmethod
    def read_temporal_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads a date or a moment - a moment in its type's time zone, in UTC without one.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        if name in {ClickhouseTypeName.DATE, ClickhouseTypeName.DATE32}:
            width = 2 if name == ClickhouseTypeName.DATE else 4
            days = int.from_bytes(
                blob[position : position + width], "little", signed=name == ClickhouseTypeName.DATE32
            )
            return CLICKHOUSE_BINARY_EPOCH_DATE + datetime.timedelta(days=days), position + width
        zone_position = 0 if name == ClickhouseTypeName.DATETIME else 1
        zone = (
            zoneinfo.ZoneInfo(ClickhouseSqlParts.get_string_text(arguments[zone_position].strip()))
            if len(arguments) > zone_position
            else datetime.UTC
        )
        if name == ClickhouseTypeName.DATETIME:
            seconds = int.from_bytes(blob[position : position + 4], "little")
            microseconds, position = seconds * 1_000_000, position + 4
        else:
            ticks = int.from_bytes(blob[position : position + 8], "little", signed=True)
            microseconds, position = ticks * 1_000_000 // 10 ** int(arguments[0]), position + 8
        moment = CLICKHOUSE_BINARY_EPOCH_MOMENT + datetime.timedelta(microseconds=microseconds)
        return moment.astimezone(zone), position

    @staticmethod
    def read_decimal_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads a decimal.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        width = ClickhouseSharedVariantValues.get_decimal_width(int(arguments[0]))[1]
        unscaled = int.from_bytes(blob[position : position + width], "little", signed=True)
        parts = Decimal(unscaled).as_tuple()
        return Decimal((parts.sign, parts.digits, -int(arguments[1]))), position + width

    @staticmethod
    def read_identifier_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads a UUID or an IP address.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        if name == ClickhouseTypeName.UUID:
            high = int.from_bytes(blob[position : position + 8], "little")
            low = int.from_bytes(blob[position + 8 : position + 16], "little")
            return uuid.UUID(int=(high << 64) | low), position + 16
        if name == ClickhouseTypeName.IPV4:
            return ipaddress.IPv4Address(int.from_bytes(blob[position : position + 4], "little")), position + 4
        return ipaddress.IPv6Address(bytes(blob[position : position + 16])), position + 16

    @staticmethod
    def read_container_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads an array, a tuple or a map.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        read_value = ClickhouseSharedVariantValues.read_value
        if name == ClickhouseTypeName.TUPLE:
            elements = []
            for argument in arguments:
                element, position = read_value(ClickhousePhysicalType.get_element(argument)[1].sql, blob, position)
                elements.append(element)
            return tuple(elements), position
        count, position = ClickhouseSharedVariantValues.read_varint(blob, position)
        if name == ClickhouseTypeName.ARRAY:
            elements = []
            for _ in range(count):
                element, position = read_value(arguments[0], blob, position)
                elements.append(element)
            return elements, position
        items = {}
        for _ in range(count):
            key, position = read_value(arguments[0], blob, position)
            item, position = read_value(arguments[1], blob, position)
            items[key] = item
        return items, position

    @staticmethod
    def read_enum_value(name: str, arguments: Sequence[str], blob: bytes, position: int) -> tuple[Any, int]:
        """Reads an enum's member - its label, its number when the type names no such member.

        Args:
            name: The type's name.
            arguments: Its arguments.
            blob: The bytes.
            position: Where the value starts.

        Returns:
            The value, and where the bytes after it start.
        """
        width = 1 if name == ClickhouseTypeName.ENUM8 else 2
        number = int.from_bytes(blob[position : position + width], "little", signed=True)
        labels = {
            member_number: label for label, member_number in ClickhousePhysicalType.get_enum_members(arguments).items()
        }
        return labels.get(number, number), position + width

    #: What writes the binary encoding of each type taking arguments, by the type's name.
    TYPE_WRITERS: ClassVar[dict[str, Callable[[str, Sequence[str], bytearray], None]]] = {
        **dict.fromkeys((ClickhouseTypeName.DATETIME, ClickhouseTypeName.DATETIME64), write_datetime_type),
        **dict.fromkeys((ClickhouseTypeName.DECIMAL, ClickhouseTypeName.FIXED_STRING), write_sized_type),
        **dict.fromkeys(
            (
                ClickhouseTypeName.ARRAY,
                ClickhouseTypeName.NULLABLE,
                ClickhouseTypeName.LOW_CARDINALITY,
                ClickhouseTypeName.MAP,
            ),
            write_wrapper_type,
        ),
        ClickhouseTypeName.TUPLE: write_tuple_type,
        **dict.fromkeys((ClickhouseTypeName.ENUM8, ClickhouseTypeName.ENUM16), write_enum_type),
    }
    #: What writes a value of each type, by the type's name.
    VALUE_WRITERS: ClassVar[dict[str, Callable[[Any, str, Sequence[str], bytearray], None]]] = {
        **dict.fromkeys(
            (ClickhouseTypeName.NULLABLE, ClickhouseTypeName.LOW_CARDINALITY, ClickhouseTypeName.NOTHING),
            write_wrapped_value,
        ),
        **dict.fromkeys(
            (
                *CLICKHOUSE_BINARY_INTEGER_WIDTHS,
                ClickhouseTypeName.BOOL,
                ClickhouseTypeName.FLOAT32,
                ClickhouseTypeName.FLOAT64,
            ),
            write_number_value,
        ),
        **dict.fromkeys((ClickhouseTypeName.STRING, ClickhouseTypeName.FIXED_STRING), write_text_value),
        **dict.fromkeys(
            (
                ClickhouseTypeName.DATE,
                ClickhouseTypeName.DATE32,
                ClickhouseTypeName.DATETIME,
                ClickhouseTypeName.DATETIME64,
            ),
            write_temporal_value,
        ),
        ClickhouseTypeName.DECIMAL: write_decimal_value,
        **dict.fromkeys(
            (ClickhouseTypeName.UUID, ClickhouseTypeName.IPV4, ClickhouseTypeName.IPV6), write_identifier_value
        ),
        **dict.fromkeys(
            (ClickhouseTypeName.ARRAY, ClickhouseTypeName.TUPLE, ClickhouseTypeName.MAP), write_container_value
        ),
        **dict.fromkeys((ClickhouseTypeName.ENUM8, ClickhouseTypeName.ENUM16), write_enum_value),
    }
    #: What reads a value of each type, by the type's name.
    VALUE_READERS: ClassVar[dict[str, Callable[[str, Sequence[str], bytes, int], tuple[Any, int]]]] = {
        **dict.fromkeys(
            (ClickhouseTypeName.NULLABLE, ClickhouseTypeName.LOW_CARDINALITY, ClickhouseTypeName.NOTHING),
            read_wrapped_value,
        ),
        **dict.fromkeys(
            (
                *CLICKHOUSE_BINARY_INTEGER_WIDTHS,
                ClickhouseTypeName.BOOL,
                ClickhouseTypeName.FLOAT32,
                ClickhouseTypeName.FLOAT64,
            ),
            read_number_value,
        ),
        **dict.fromkeys((ClickhouseTypeName.STRING, ClickhouseTypeName.FIXED_STRING), read_text_value),
        **dict.fromkeys(
            (
                ClickhouseTypeName.DATE,
                ClickhouseTypeName.DATE32,
                ClickhouseTypeName.DATETIME,
                ClickhouseTypeName.DATETIME64,
            ),
            read_temporal_value,
        ),
        ClickhouseTypeName.DECIMAL: read_decimal_value,
        **dict.fromkeys(
            (ClickhouseTypeName.UUID, ClickhouseTypeName.IPV4, ClickhouseTypeName.IPV6), read_identifier_value
        ),
        **dict.fromkeys(
            (ClickhouseTypeName.ARRAY, ClickhouseTypeName.TUPLE, ClickhouseTypeName.MAP), read_container_value
        ),
        **dict.fromkeys((ClickhouseTypeName.ENUM8, ClickhouseTypeName.ENUM16), read_enum_value),
    }
