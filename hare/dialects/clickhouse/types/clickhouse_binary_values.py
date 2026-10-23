from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.models import Model
    from hare.sql.terms.term import Term


class ClickhouseBinaryValues:
    """How ClickHouse stores bytes - a ``String`` column of their lowercase hex. A library reading the
    server's ``String`` as text can't give bytes back: clickhouse-connect writes bytes that aren't
    UTF-8 as their hex, so raw bytes and a text of hex digits read the same. The hex reads back
    exactly over both drivers, orders as the bytes do, and ``unhex()`` gives the bytes in SQL."""

    @staticmethod
    def to_db(field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The bytes' hex.

        Args:
            field: The binary field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The hex, None for None.
        """
        data = field.to_db_value(value, instance)
        return None if data is None else bytes(data).hex()

    @staticmethod
    def to_python(field: Field[Any], value: Any) -> Any:
        """The bytes of a stored hex.

        Args:
            field: The binary field.
            value: The stored hex - text, or bytes from a driver reading it so.

        Returns:
            The bytes, None for None.
        """
        if value is None:
            return None
        return bytes.fromhex(value.decode() if isinstance(value, (bytes, bytearray)) else value)

    @staticmethod
    def get_json_term(field: Field[Any], term: Term) -> Term:
        """The text a column's bytes go into a JSON object as - the hex the column holds.

        Args:
            field: The binary field.
            term: The column.

        Returns:
            The column.
        """
        return term
