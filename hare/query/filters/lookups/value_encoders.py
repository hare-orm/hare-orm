from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import UnSupportedError, ValidationError
from hare.fields.constants import NULL_BYTE_MESSAGE
from hare.fields.data.constants import INT64_MAX, INT64_MIN

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.models import Model


class ValueEncoders:
    """The encoders turning a lookup's Python value into what its operator compares with - each
    called as ``(value, model, field, dialect)``, ``dialect`` being the one the query runs on."""

    @staticmethod
    def prepare(encoder: Callable[..., Any], field: Field[Any], types: TypeRegistry) -> Callable[..., Any]:
        """Bind standard lookup conversions once; extension encoders keep their contract."""
        if encoder is ValueEncoders.encode_list:
            return partial(encoder, convert=types.get_lookup_values_writer(field))
        if encoder is ValueEncoders.encode_related_value or encoder is ValueEncoders.encode_related_list:
            key_field = field.related_model._meta.pk  # type: ignore[attr-defined]
            return partial(encoder, writer=types.get_db_writer(key_field))
        if encoder is ValueEncoders.encode_composite_pk or encoder is ValueEncoders.encode_composite_related_list:
            fields = field.related_model._meta.pk_fields  # type: ignore[attr-defined]
            return partial(encoder, writers=tuple(types.get_db_writer(column) for column in fields))
        return encoder

    @staticmethod
    def encode_list(
        values: Iterable[Any],
        obj: Model | type[Model],
        field: Field[Any],
        dialect: Dialect,
        *,
        convert: Callable[[list[Any], Any], list[Any]] | None = None,
    ) -> list[Any]:
        """Encodes the values of ``__in``/``__not_in``/``__range``. A None is passed through - the
        lookup turns it into a NULL check. Each other value goes through ``to_lookup_value()``, not
        the write conversion, which could change the value compared.

        Raises:
            UnSupportedError: ``values`` isn't a list, tuple or set.
        """
        if not isinstance(values, (list, tuple, set)):
            raise UnSupportedError(
                f"{field.model_field_name}: expected a list/tuple/set of values for this lookup, got "
                f"{field.get_value_for_message(values)}"
            )
        convert = dialect.types.get_lookup_values_writer(field) if convert is None else convert
        return convert(list(values), obj)

    @staticmethod
    def encode_related_value(
        value: Any,
        obj: Model,
        field: Field[Any],
        dialect: Dialect,
        *,
        writer: Callable[[Any, Any], Any] | None = None,
    ) -> Any:
        """Encodes a related object/pk for a many-to-many or backward relation filter as its key
        column binds it.

        Args:
            value: A related obj or its primary key.
            obj: The model the filter is built for.
            field: The relation.
            dialect: The dialect the query runs on.

        Returns:
            The value to bind.
        """
        if writer is None:
            writer = dialect.types.get_db_writer(field.related_model._meta.pk)  # type: ignore[attr-defined]
        return writer(value, obj)

    @staticmethod
    def encode_related_list(
        values: Iterable[Any],
        obj: Model,
        field: Field[Any],
        dialect: Dialect,
        *,
        writer: Callable[[Any, Any], Any] | None = None,
    ) -> list[Any]:
        """Encodes related objects or primary keys for a many-to-many or backward relation filter. A
        None is rejected: over a relation it would mean "no related row", which ``__isnull`` asks.

        Raises:
            UnSupportedError: ``values`` contains None.
        """
        if any(element is None for element in values):
            raise UnSupportedError(
                "None is not supported inside __in=/__not_in= for a many-to-many or backward "
                "relation filter - use __isnull=/__not_isnull= to filter on whether the relation "
                "itself is empty."
            )
        if writer is None:
            writer = dialect.types.get_db_writer(field.related_model._meta.pk)  # type: ignore[attr-defined]
        return [writer(getattr(element, "pk", element), obj) for element in values]

    @staticmethod
    def encode_composite_pk(
        value: Any,
        obj: Model,
        field: Field[Any],
        dialect: Dialect,
        *,
        writers: tuple[Callable[[Any, Any], Any], ...] | None = None,
    ) -> tuple[Any, ...]:
        """Encodes a single composite-PK value (a related model obj, or its raw ``pk`` tuple
        directly) for a many-to-many filter targeting a composite-PK model, into a tuple of
        database-compatible values - one per component, in primary key order.

        Raises:
            UnSupportedError: ``value``'s raw tuple doesn't have one element per key field.
        """
        pk_fields = field.related_model._meta.pk_fields  # type: ignore[attr-defined]
        raw = getattr(value, "pk", value)
        if not isinstance(raw, tuple) or len(raw) != len(pk_fields):
            raise UnSupportedError(
                f"expected a {len(pk_fields)}-tuple primary key (or an instance with one), got {raw!r}."
            )
        if writers is None:
            writers = tuple(dialect.types.get_db_writer(column) for column in pk_fields)
        return tuple(write(component, obj) for write, component in zip(writers, raw, strict=True))

    @staticmethod
    def encode_composite_related_list(
        values: Iterable[Any],
        obj: Model,
        field: Field[Any],
        dialect: Dialect,
        *,
        writers: tuple[Callable[[Any, Any], Any], ...] | None = None,
    ) -> list[tuple[Any, ...]]:
        """``ValueEncoders.encode_composite_pk``, applied to every element of an iterable for ``__in``/``__not_in``
        on a many-to-many filter targeting a composite-PK model. See ``ValueEncoders.encode_related_list`` for why
        ``None`` is rejected outright rather than passed through.

        Raises:
            UnSupportedError: See ``ValueEncoders.encode_related_list``/``ValueEncoders.encode_composite_pk``.
        """
        if any(element is None for element in values):
            raise UnSupportedError(
                "None is not supported inside __in=/__not_in= for a many-to-many relation filter - "
                "use __isnull=/__not_isnull= to filter on whether the relation itself is empty."
            )
        if writers is None:
            writers = tuple(dialect.types.get_db_writer(column) for column in field.related_model._meta.pk_fields)  # type: ignore[attr-defined]
        return [ValueEncoders.encode_composite_pk(element, obj, field, dialect, writers=writers) for element in values]

    @staticmethod
    def encode_bool(value: Any, obj: Model | None, field: Field[Any], dialect: Dialect) -> bool:
        """Returns ``value`` when it is a bool - a truthy string like "false" would invert the filter.

        Raises:
            UnSupportedError: ``value`` isn't a bool.
        """
        if not isinstance(value, bool):
            raise UnSupportedError(
                f"{field.model_field_name}: {field.get_value_for_message(value)} is not supported for this lookup - "
                "a bool is required."
            )
        return value

    @staticmethod
    def encode_string(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> str:
        """Returns ``value`` as text for a LIKE or regex lookup. A None is rejected - it would search
        for the text "None"; ``__isnull`` filters on NULL.

        Raises:
            UnSupportedError: ``value`` is None.
            ValidationError: ``value`` isn't a str (its text would be searched for), or contains a null
                byte.
        """
        if value is None:
            raise UnSupportedError(
                f"{field.model_field_name}: None is not supported for this lookup - "
                "use __isnull=/__not_isnull= to filter on whether the field is NULL."
            )
        # A value member mixed in with str (`class E(str, enum.Enum)`) IS a str by isinstance, but
        # Python 3.12+'s own Enum.__str__ renders it as "ClassName.member_name", not its actual
        # string value - unwrapped to `.value` first so `str()` below only ever sees a plain string.
        if isinstance(value, Enum):
            value = value.value
        if not isinstance(value, str):
            raise ValidationError(f"{field.model_field_name}: this lookup matches a text, got {type(value).__name__}")
        text = str(value)
        if "\x00" in text and dialect.features.cuts_like_pattern_at_null_byte:
            # The database cuts a LIKE pattern at the null byte - `'%\x00x%'` would match every row.
            raise ValidationError(f"{field.model_field_name}: {NULL_BYTE_MESSAGE}")
        return text

    @staticmethod
    def encode_string_list(values: Iterable[Any], obj: Model, field: Field[Any], dialect: Dialect) -> list[str]:
        """Encodes the key list of a JSON `__has_keys`/`__has_any_keys` lookup - every element goes
        through `ValueEncoders.encode_string`, a bare string is rejected (it would otherwise be iterated per
        character).

        Raises:
            UnSupportedError: `values` is a string, or isn't iterable.
        """
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
            raise UnSupportedError(
                f"{field.model_field_name}: {field.get_value_for_message(values)} is not supported for this lookup - "
                "a list of keys is required."
            )
        return [ValueEncoders.encode_string(value, obj, field, dialect) for value in values]

    @staticmethod
    def encode_int(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> int:
        """An integer a lookup compares with - a length.

        Raises:
            ValidationError: ``value`` is no integer of 64 bits.
        """
        if isinstance(value, bool) or not isinstance(value, int) or not INT64_MIN <= value <= INT64_MAX:
            raise ValidationError(f"{field.model_field_name}: this lookup takes an integer, got {value!r}")
        return value

    @staticmethod
    def encode_json(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> dict[str, Any]:
        """Passes the dict of a JSON ``__filter`` on as it is - the lookup reads its keys itself."""
        return value
