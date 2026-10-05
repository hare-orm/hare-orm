from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import UnSupportedError, ValidationError
from hare.fields import Field
from hare.fields.constants import NULL_BYTE_MESSAGE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class ValueEncoders:
    """The encoders turning a lookup's Python value into what its operator compares with - each
    called as ``(value, model, field, dialect)``, ``dialect`` being the one the query runs on."""

    @staticmethod
    def encode_list(values: Iterable[Any], obj: Model | type[Model], field: Field[Any], dialect: Dialect) -> list[Any]:
        """Encodes the values of ``__in``/``__not_in``/``__range``. A None is passed through - the
        lookup turns it into a NULL check. Each other value goes through ``to_lookup_value()``, not
        the write conversion, which could change the value compared.

        Raises:
            UnSupportedError: ``values`` isn't a list, tuple or set.
        """
        if not isinstance(values, (list, tuple, set)):
            raise UnSupportedError(
                f"{field.model_field_name}: expected a list/tuple/set of values for this lookup, got {values!r}"
            )
        # The conversion TypeRegistry.get_lookup_value() picks, picked once for the whole list.
        converter = dialect.types.get_lookup_converter(type(field))
        if converter is None:
            # Imported here: the accelerator's module imports the package of this one.
            from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

            list_writer = HydrateAccelerator.get_lookup_list_writer(field, dialect.types)
            if list_writer is not None:
                write_list, value_type = list_writer
                written_values = write_list(values, obj, value_type, False)
                if written_values is not None:
                    return written_values
            to_lookup_value = field.to_lookup_value
            return [element if element is None else to_lookup_value(element, obj) for element in values]
        return [element if element is None else converter(field, element, obj) for element in values]

    @staticmethod
    def encode_bound_list(
        values: list[Any] | tuple[Any, ...] | set[Any], obj: Model | type[Model], field: Field[Any], dialect: Dialect
    ) -> tuple[list[Any], bool]:
        """``encode_list()`` of a list bound as one parameter - its values but None, which the
        statement tests with IS NULL.

        Args:
            values: The list, tuple or set.
            obj: The model the filter is built for.
            field: The field compared.
            dialect: The dialect the query runs on.

        Returns:
            The values, and whether the field's native codec wrote them all from its one value type
            - none of them is a SQL term then.
        """
        types = dialect.types
        if types.get_lookup_converter(type(field)) is None:
            # Imported here: the accelerator's module imports the package of this one.
            from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

            list_writer = HydrateAccelerator.get_lookup_list_writer(field, types)
            if list_writer is not None:
                write_list, value_type = list_writer
                written_values = write_list(values, obj, value_type, True)
                if written_values is not None:
                    return written_values, value_type is not None
        encoded_values = ValueEncoders.encode_list(values, obj, field, dialect)
        return [encoded_value for encoded_value in encoded_values if encoded_value is not None], False

    @staticmethod
    def encode_related_value(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Any:
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
        return dialect.types.get_db_value(cast("RelationalField[Any]", field).related_model._meta.pk, value, obj)

    @staticmethod
    def encode_related_list(values: Iterable[Any], obj: Model, field: Field[Any], dialect: Dialect) -> list[Any]:
        """Encodes related objects or primary keys for a many-to-many or backward relation filter. A
        None is rejected: over a relation it would mean "no related row", which ``__isnull`` asks.

        Raises:
            UnSupportedError: ``values`` contains None.
        """
        target_pk = field.related_model._meta.pk  # type: ignore[attr-defined]
        types = dialect.types
        # Imported here: the accelerator's module imports the package of this one.
        from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

        list_writer = HydrateAccelerator.get_lookup_list_writer(target_pk, types)
        if list_writer is not None and list_writer[1] is not None:
            # Keys all of the one type the native codec converts - written at once. The written
            # keys are of that type or None, which no key equals.
            written_values = list_writer[0](values, obj, list_writer[1], False)
            if written_values is not None and None not in written_values:
                return written_values
        if any(element is None for element in values):
            raise UnSupportedError(
                "None is not supported inside __in=/__not_in= for a many-to-many or backward "
                "relation filter - use __isnull=/__not_isnull= to filter on whether the relation "
                "itself is empty."
            )
        # A key of the type the native codec converts, as get_db_value() would - the keys of a
        # prefetch are many.
        native_convert, native_value_type = HydrateAccelerator.get_lookup_writer(target_pk, types) or (None, None)
        encoded = []
        for element in values:
            if native_convert is not None and type(element) is native_value_type:
                encoded.append(native_convert(element, obj))
            else:
                encoded.append(types.get_db_value(target_pk, element.pk if hasattr(element, "pk") else element, obj))
        return encoded

    @staticmethod
    def encode_composite_pk(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> tuple[Any, ...]:
        """Encodes a single composite-PK value (a related model obj, or its raw ``pk`` tuple
        directly) for a many-to-many filter targeting a composite-PK model, into a tuple of
        database-compatible values - one per component, in primary key order.

        Raises:
            UnSupportedError: ``value``'s raw tuple doesn't have one element per key field.
        """
        pk_fields = cast("RelationalField[Any]", field).related_model._meta.pk_fields
        raw = value.pk if hasattr(value, "pk") else value
        if not isinstance(raw, tuple) or len(raw) != len(pk_fields):
            raise UnSupportedError(
                f"expected a {len(pk_fields)}-tuple primary key (or an instance with one), got {raw!r}."
            )
        return tuple(
            dialect.types.get_db_value(pk_field, component, obj)
            for pk_field, component in zip(pk_fields, raw, strict=True)
        )

    @staticmethod
    def encode_composite_related_list(
        values: Iterable[Any], obj: Model, field: Field[Any], dialect: Dialect
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
        return [ValueEncoders.encode_composite_pk(element, obj, field, dialect) for element in values]

    @staticmethod
    def encode_bool(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> bool:
        """Returns ``value`` when it is a bool - a truthy string like "false" would invert the filter.

        Raises:
            UnSupportedError: ``value`` isn't a bool.
        """
        if not isinstance(value, bool):
            raise UnSupportedError(
                f"{field.model_field_name}: {value!r} is not supported for this lookup - a bool is required."
            )
        return value

    @staticmethod
    def encode_string(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> str:
        """Returns ``value`` as text for a LIKE or regex lookup. A None is rejected - it would search
        for the text "None"; ``__isnull`` filters on NULL.

        Raises:
            UnSupportedError: ``value`` is None.
            ValidationError: ``value`` contains a null byte.
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
        text = str(value)
        if "\x00" in text:
            # SQLite cuts a LIKE pattern at the null byte (`'%\x00x%'` matches every row) and Postgres
            # can't receive one - refused on every backend, the way an equality filter refuses it.
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
                f"{field.model_field_name}: {values!r} is not supported for this lookup - a list of keys is required."
            )
        return [ValueEncoders.encode_string(value, obj, field, dialect) for value in values]

    @staticmethod
    def encode_int(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> int:
        return int(value)

    @staticmethod
    def encode_json(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> dict[str, Any]:
        return value
