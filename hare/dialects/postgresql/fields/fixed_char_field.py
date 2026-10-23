from __future__ import annotations

from typing import TypeVar

from hare.fields.data.text.char_field import CharField

TStr = TypeVar("TStr", str, str | None)


class FixedCharField(CharField[TStr]):
    """A ``character(max_length)`` - a text of at most ``max_length`` characters PostgreSQL pads with
    spaces to ``max_length``: it is read back padded, and compared ignoring the trailing spaces.
    ``CharField`` writes a ``varchar``.

    Args:
        max_length: The characters the column holds.
    """

    COLUMN_TYPE_FROM_DIALECT = True

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"CHAR({self.max_length})"
