"""BinaryField."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TBinary = TypeVar("TBinary", bytes, bytes | None)


class BinaryField(Field[TBinary]):
    """A ``bytes`` value - ``bytea`` on PostgreSQL, ``BLOB`` on SQLite. ``bytearray``/``memoryview``
    are taken as bytes; an ``int`` is refused, not turned into zero bytes.

    It filters by equality, ``__in``, ``__isnull`` and the comparisons (byte order), is set by
    ``update()``, and takes ``unique=True`` / ``db_index=True`` - a B-tree index, so on PostgreSQL a
    value of more than about 2.7 KB can't be indexed (index a hash of it instead).
    """

    field_type = bytes

    SQL_TYPE = "BLOB"

    @overload
    def __init__(self: BinaryField[bytes], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: BinaryField[bytes | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        # bytes(5) means five zero bytes - an int is rejected.
        if isinstance(value, int):
            raise ValidationError(
                f"{self.model_field_name}: expected bytes-like data, got the int {self.get_value_for_message(value)}"
            )
        return super().to_db_value(value, instance)

    def to_python(self, value: Any) -> Any:
        # The same on construction.
        if isinstance(value, int):
            raise ValidationError(
                f"{self.model_field_name}: expected bytes-like data, got the int {self.get_value_for_message(value)}"
            )
        return super().to_python(value)
