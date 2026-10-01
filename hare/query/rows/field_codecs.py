from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from hare.fields.base.field import Field
from hare.fields.data.binary import BinaryField
from hare.fields.data.boolean import BooleanField
from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance
from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
from hare.fields.data.json.json_codec import JsonCodec
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.uuids import UUIDField
from hare.fields.validators.max_digits_validator import MaxDigitsValidator
from hare.fields.validators.max_length_validator import MaxLengthValidator
from hare.fields.validators.max_value_validator import MaxValueValidator
from hare.fields.validators.min_length_validator import MinLengthValidator
from hare.fields.validators.min_value_validator import MinValueValidator
from hare.models.enums import FieldBucket
from hare.query.rows.constants import INLINE_CHECK_VALIDATOR_TYPES, INLINE_VALUE_CHECK_TYPES
from hare.query.rows.enums import InlineCheckKind, ReadCodecKind, WriteCodecKind
from hare.utils import Timezone
from hare.utils.constants import DEFAULT_TIMEZONE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.query.rows.hydration_layout import HydrationEntry

#: A codec kind and the options it is built with.
type CodecSpec[Kind] = tuple[Kind, dict[str, Any]]


class FieldCodecs:
    """Decides how ``rust.native.rows.FieldCodec`` reads and writes the column of a field: the codec
    of its type when the field converts values exactly as its type's base class does, the field's
    own Python method otherwise. A codec takes the usual shapes of its type itself and hands any other
    value to the field, so a value always gets what the field's method would give it."""

    @staticmethod
    def overrides_read_conversion(field: Field[Any], base_class: type[Field[Any]]) -> bool:
        """Whether ``field``'s class reads values otherwise than ``base_class``."""
        field_class = type(field)
        return (
            field_class.from_db_value is not base_class.from_db_value
            or field_class.to_python is not base_class.to_python
        )

    @staticmethod
    def overrides_write_conversion(field: Field[Any], base_class: type[Field[Any]]) -> bool:
        """Whether ``field``'s class writes values otherwise than ``base_class``."""
        return type(field).to_db_value is not base_class.to_db_value

    @staticmethod
    def is_plain_attribute(field: Field[Any]) -> bool:
        """Whether assigning the field on an instance of its model only stores the value and drops a
        pending async default - the model's ``__setattr__`` is ``Model.__setattr__`` and has nothing
        more to do for the field.

        Args:
            field: The field.

        Returns:
            True when a codec may store the value itself.
        """
        # Imported here: the Model module imports the rows package.
        from hare.models.model import Model

        model = field.model
        if model is None or model.__setattr__ is not Model.__setattr__:
            return False
        meta = model._meta
        return field.model_field_name not in meta.get_setattr_hooks() and meta.next_setattr is object.__setattr__

    @staticmethod
    def get_zone(zone_name: str | None) -> tuple[Any, bool]:
        """The configured zone and whether it is UTC, for a zone name - None without ``use_tz``."""
        if zone_name is None:
            return None, False
        zone = Timezone.parse(zone_name)
        return zone, getattr(zone, "key", None) == DEFAULT_TIMEZONE

    @staticmethod
    def get_read_spec(entry: HydrationEntry, types: TypeRegistry, zone_name: str | None) -> CodecSpec[ReadCodecKind]:
        """How a column is read.

        Args:
            entry: The column's hydration entry.
            types: The dialect's type registry.
            zone_name: The configured zone under ``use_tz``, else None.

        Returns:
            The read codec kind and its options.
        """
        _name, field, bucket, dialect_reader = entry
        use_tz = zone_name is not None
        if dialect_reader is not None:
            if (
                isinstance(field, DatetimeField)
                and types.reads_naive_datetime_as_utc(type(field))
                and not FieldCodecs.overrides_read_conversion(field, DatetimeField)
            ):
                return FieldCodecs.get_datetime_read_spec(dialect_reader, zone_name, naive_is_utc=True)
            return ReadCodecKind.CALL, {"reader": dialect_reader}
        if bucket == FieldBucket.NATIVE:
            return ReadCodecKind.AS_IS, {}
        if bucket == FieldBucket.DEFAULT:
            return ReadCodecKind.FIELD_TYPE, {"field_type": field.field_type}
        own_spec = field.get_read_codec_spec(types, zone_name)
        if own_spec is not None:
            return cast("CodecSpec[ReadCodecKind]", own_spec)
        fallback = field.from_db_value
        if isinstance(field, DatetimeField) and not FieldCodecs.overrides_read_conversion(field, DatetimeField):
            return FieldCodecs.get_datetime_read_spec(fallback, zone_name, naive_is_utc=False)
        if isinstance(field, TimeField) and not FieldCodecs.overrides_read_conversion(field, TimeField):
            return ReadCodecKind.TIME, {
                "fallback": fallback,
                "use_tz": use_tz,
                # A fixed offset: a bare time has no date to resolve a named zone's offset against.
                "fixed_offset": Timezone.get_fixed_offset(zone_name) if use_tz else None,
            }
        simple_kinds: tuple[tuple[type[Field[Any]], ReadCodecKind], ...] = (
            (DateField, ReadCodecKind.DATE),
            (TimeDeltaField, ReadCodecKind.TIMEDELTA),
            (UUIDField, ReadCodecKind.UUID),
            (BooleanField, ReadCodecKind.BOOLEAN),
            (BinaryField, ReadCodecKind.BINARY),
        )
        for field_class, kind in simple_kinds:
            if isinstance(field, field_class) and not FieldCodecs.overrides_read_conversion(field, field_class):
                return kind, {"fallback": fallback}
        for enum_field_class in (IntEnumFieldInstance, CharEnumFieldInstance):
            if isinstance(field, enum_field_class) and not FieldCodecs.overrides_read_conversion(
                field, enum_field_class
            ):
                return ReadCodecKind.ENUMERATION, {
                    "members_by_value": cast("type[Enum]", field.enum_type)._value2member_map_,
                    "fallback": fallback,
                }
        if isinstance(field, DecimalField) and not FieldCodecs.overrides_read_conversion(field, DecimalField):
            return ReadCodecKind.DECIMAL, {
                "quant": field.quant,
                "context": field.quantize_context,
                "fallback": fallback,
            }
        if isinstance(field, JSONField) and not FieldCodecs.overrides_read_conversion(field, JSONField):
            type_adapter = field.get_value_type_adapter()
            return ReadCodecKind.JSON, {
                "decoder": field.decoder,
                # The scan JsonCodec.loads makes runs natively; text without a long integer goes
                # to orjson directly.
                "fast_decoder": (
                    JsonCodec.orjson.loads
                    if field.decoder is JsonCodec.loads and JsonCodec.orjson is not None
                    else None
                ),
                "validate_type": type_adapter.validate_python if type_adapter is not None else None,
                "fallback": fallback,
            }
        return ReadCodecKind.CALL, {"reader": fallback}

    @staticmethod
    def get_expression_read_spec(
        name: str, field: Field[Any], types: TypeRegistry, zone_name: str | None
    ) -> CodecSpec[ReadCodecKind]:
        """How a column computed by an expression - an annotation decoded through ``field`` - is read:
        always as the field reads it, since the driver may return another type than a column of the
        field holds.

        Args:
            name: The column's name.
            field: The field the value is decoded through.
            types: The dialect's type registry.
            zone_name: The configured zone under ``use_tz``, else None.

        Returns:
            The read codec kind and its options.
        """
        dialect_reader = types.get_python_reader(field) if types.get_python_converter(type(field)) else None
        kind, options = FieldCodecs.get_read_spec((name, field, FieldBucket.COMPLEX, dialect_reader), types, zone_name)
        # A union of types (an int | str field) has no one exact type.
        field_type: Any = field.field_type
        keeps_exact_type = not FieldCodecs.overrides_read_conversion(field, Field) or (
            isinstance(field, IntField) and not FieldCodecs.overrides_read_conversion(field, IntField)
        )
        if kind == ReadCodecKind.CALL and dialect_reader is None and keeps_exact_type and isinstance(field_type, type):
            # Field.to_python and IntField's keep a value of exactly the field's type as it is.
            return ReadCodecKind.EXACT_TYPE, {"exact_type": field_type, "reader": options["reader"]}
        return kind, options

    @staticmethod
    def get_datetime_read_spec(
        fallback: Callable[[Any], Any], zone_name: str | None, *, naive_is_utc: bool
    ) -> CodecSpec[ReadCodecKind]:
        """How a datetime column is read.

        Args:
            fallback: The reader of a value the codec leaves to Python.
            zone_name: The configured zone under ``use_tz``, else None.
            naive_is_utc: A naive datetime from the driver is a UTC instant.

        Returns:
            The read codec kind and its options.
        """
        zone, zone_is_utc = FieldCodecs.get_zone(zone_name)
        return ReadCodecKind.DATETIME, {
            "fallback": fallback,
            "use_tz": zone_name is not None,
            "zone": zone,
            "zone_is_utc": zone_is_utc,
            "naive_is_utc": naive_is_utc,
        }

    @staticmethod
    def get_inline_checks(field: Field[Any]) -> list[tuple[InlineCheckKind, Any]] | None:
        """The field's validators as checks the codec makes itself, in their order.

        Args:
            field: The field.

        Returns:
            The checks; None when a validator is of another kind or has a message of its own, and
            ``field.validate()`` checks the value.
        """
        checks: list[tuple[InlineCheckKind, Any]] = []
        is_number = field.field_type in INLINE_VALUE_CHECK_TYPES
        for validator in field.validators:
            # A subclass may check otherwise.
            if type(validator) not in INLINE_CHECK_VALIDATOR_TYPES or getattr(validator, "message", None) is not None:
                return None
            if isinstance(validator, MinValueValidator) and is_number:
                checks.append((InlineCheckKind.MIN_VALUE, validator.min_value))
            elif isinstance(validator, MaxValueValidator) and is_number:
                checks.append((InlineCheckKind.MAX_VALUE, validator.max_value))
            elif isinstance(validator, MinLengthValidator) and field.field_type is str:
                checks.append((InlineCheckKind.MIN_LENGTH, validator.min_length))
            elif isinstance(validator, MaxLengthValidator) and field.field_type is str:
                checks.append((InlineCheckKind.MAX_LENGTH, validator.max_length))
            elif (
                isinstance(validator, MaxDigitsValidator)
                and isinstance(field, DecimalField)
                and (validator.max_digits, validator.decimal_places) == (field.max_digits, field.decimal_places)
            ):
                checks.append((InlineCheckKind.MAX_DIGITS, (field.max_digits, field.decimal_places)))
            else:
                return None
        return checks

    @staticmethod
    def get_validation_options(field: Field[Any]) -> dict[str, Any]:
        """The options a codec checks a written value with: the inline checks, or ``field.validate``."""
        checks = FieldCodecs.get_inline_checks(field)
        if checks is None:
            return {"validate": field.validate}
        return {"checks": checks}

    @staticmethod
    def get_write_spec(field: Field[Any], types: TypeRegistry, zone_name: str | None) -> CodecSpec[WriteCodecKind]:
        """How a value of a field is written.

        Args:
            field: The field.
            types: The dialect's type registry.
            zone_name: The configured zone under ``use_tz``, else None.

        Returns:
            The write codec kind and its options.
        """
        writer = types.get_db_writer(field)
        converter = types.get_db_converter(type(field))
        if field.sensitive:
            # The field's own messages hide a sensitive value.
            return WriteCodecKind.CALL, {"writer": writer}
        common: dict[str, Any] = {"fallback": writer, "null": field.null}
        validate = field.validate if field.validators else None
        if (
            isinstance(field, DatetimeField)
            and converter in (None, DatetimeField.to_db_instant_value)
            and not FieldCodecs.overrides_write_conversion(field, DatetimeField)
            and type(field).to_db_instant_value is DatetimeField.to_db_instant_value
            and not FieldCodecs.overrides_read_conversion(field, DatetimeField)
        ):
            # A column storing UTC instants differs from one storing wall clocks only without
            # use_tz, which the codec leaves to the writer.
            zone, zone_is_utc = FieldCodecs.get_zone(zone_name)
            return WriteCodecKind.DATETIME, {
                **common,
                "use_tz": zone_name is not None,
                "zone": zone,
                "zone_is_utc": zone_is_utc,
                "stores_utc_instants": converter is DatetimeField.to_db_instant_value,
                "text": DatetimeField.field_type in types.bound_as_text,
                "auto_now": field.auto_now,
                "auto_now_add": field.auto_now_add,
                "assign_directly": FieldCodecs.is_plain_attribute(field),
                "validate": validate,
            }
        if converter is not None:
            return WriteCodecKind.CALL, {"writer": writer}
        if isinstance(field, DateField) and not FieldCodecs.overrides_write_conversion(field, DateField):
            return WriteCodecKind.DATE, {
                **common,
                "text": DateField.field_type in types.bound_as_text,
                "validate": validate,
            }
        if (
            isinstance(field, TimeField)
            and not FieldCodecs.overrides_write_conversion(field, TimeField)
            and not (field.auto_now or field.auto_now_add)
        ):
            return WriteCodecKind.TIME, {
                **common,
                "use_tz": zone_name is not None,
                "text": TimeField.field_type in types.bound_as_text,
                "validate": validate,
            }
        if isinstance(field, TimeDeltaField) and not FieldCodecs.overrides_write_conversion(field, TimeDeltaField):
            return WriteCodecKind.TIMEDELTA, {**common, "validate": validate}
        if isinstance(field, UUIDField) and not FieldCodecs.overrides_write_conversion(field, UUIDField):
            return WriteCodecKind.UUID, {**common, "validate": validate}
        if isinstance(field, IntEnumFieldInstance) and not FieldCodecs.overrides_write_conversion(
            field, IntEnumFieldInstance
        ):
            return WriteCodecKind.ENUMERATION, {
                **common,
                "enum_type": field.enum_type,
                "stored_values_by_member": {member: int(member.value) for member in field.enum_type},
                "validate": validate,
            }
        if isinstance(field, CharEnumFieldInstance) and not FieldCodecs.overrides_write_conversion(
            field, CharEnumFieldInstance
        ):
            return WriteCodecKind.ENUMERATION, {
                **common,
                "enum_type": field.enum_type,
                "stored_values_by_member": {member: str(member.value) for member in field.enum_type},
                "validate": validate,
            }
        if isinstance(field, DecimalField) and not FieldCodecs.overrides_write_conversion(field, DecimalField):
            return WriteCodecKind.DECIMAL, {
                **common,
                "quant": field.quant,
                "context": field.quantize_context,
                "decimal_places": field.decimal_places,
                "text": DecimalField.field_type in types.bound_as_text,
                **FieldCodecs.get_validation_options(field),
            }
        if (
            isinstance(field, JSONField)
            and not FieldCodecs.overrides_write_conversion(field, JSONField)
            and field.get_value_type_adapter() is None
            and field.encoder is JsonCodec.dumps
            and JsonCodec.orjson is not None
        ):
            # The codec writes the text orjson writes - with another encoder the field encodes.
            return WriteCodecKind.JSON, {**common, "validate": validate}
        if isinstance(field, BinaryField) and not FieldCodecs.overrides_write_conversion(field, BinaryField):
            return WriteCodecKind.SCALAR, {
                **common,
                "exact_type": bytes,
                **FieldCodecs.get_validation_options(field),
            }
        write_check = field.get_native_write_check()
        if isinstance(field.field_type, type) and (
            write_check is not None or not FieldCodecs.overrides_write_conversion(field, Field)
        ):
            return WriteCodecKind.SCALAR, {
                **common,
                "exact_type": field.field_type,
                "check": int(write_check) if write_check is not None else 0,
                **FieldCodecs.get_validation_options(field),
            }
        return WriteCodecKind.CALL, {"writer": writer}
