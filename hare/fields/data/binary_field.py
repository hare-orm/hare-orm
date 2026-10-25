from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ValidationError
from hare.fields.data.constants import BINARY_FIELD_SUPPORTED_LOOKUPS, BINARY_VALUE_TYPE
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.models import Model

TBinary = TypeVar("TBinary", bytes, bytes | None)


class BinaryField(Field[TBinary]):
    """A ``bytes`` value - the binary column type of the dialect. ``bytearray``/``memoryview``
    are taken as bytes; an ``int`` is refused, not turned into zero bytes.

    It filters by equality, ``__in``, ``__isnull`` and the comparisons (byte order) - any other
    lookup (``__contains``, ...) raises ``FieldError`` - is set by
    ``update()``, and takes ``unique=True`` / ``db_index=True`` - a B-tree index, which a database may
    refuse for a long value (index a hash of it instead).
    """

    argument_value_type = BINARY_VALUE_TYPE

    field_type = bytes

    SQL_TYPE = "BLOB"
    supported_lookups = BINARY_FIELD_SUPPORTED_LOOKUPS

    @overload
    def __init__(self: BinaryField[bytes], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: BinaryField[bytes | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        self.raise_if_not_bytes_like(value)
        return super().to_db_value(value, instance)

    def to_python(self, value: Any) -> Any:
        # The same on construction.
        self.raise_if_not_bytes_like(value)
        return super().to_python(value)

    def raise_if_not_bytes_like(self, value: Any) -> None:
        """Rejects a value that isn't bytes-like data - bytes() would turn an int into zero bytes and a
        list or a dict into the bytes of its items or keys.

        Args:
            value: The value.

        Raises:
            ValidationError: The value is neither None, bytes, a bytearray nor a memoryview.
        """
        if value is not None and not isinstance(value, (bytes, bytearray, memoryview)):
            raise ValidationError(
                f"{self.model_field_name}: expected bytes-like data, got {ValueDescriptions.get_description(value)}"
            )
