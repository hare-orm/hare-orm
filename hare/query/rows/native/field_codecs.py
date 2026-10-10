from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from hare.fields.data.binary_field import BinaryField
from hare.fields.data.boolean_field import BooleanField
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
from hare.fields.data.text.email_field import EmailField
from hare.fields.data.text.phone_field import PhoneField
from hare.fields.data.text.slug_field import SlugField
from hare.fields.data.text.url_field import URLField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field
from hare.fields.validators.formats.e164_phone_validator import E164PhoneValidator
from hare.fields.validators.formats.email_validator import EmailValidator
from hare.fields.validators.formats.slug_validator import SlugValidator
from hare.fields.validators.formats.url_validator import URLValidator
from hare.fields.validators.limits.max_digits_validator import MaxDigitsValidator
from hare.fields.validators.limits.max_length_validator import MaxLengthValidator
from hare.fields.validators.limits.max_value_validator import MaxValueValidator
from hare.fields.validators.limits.min_length_validator import MinLengthValidator
from hare.fields.validators.limits.min_value_validator import MinValueValidator
from hare.models.enums import FieldBucket
from hare.query.rows.constants import INLINE_CHECK_VALIDATOR_TYPES, INLINE_VALUE_CHECK_TYPES
from hare.query.rows.enums import InlineCheckType, ReadCodecType, TextFormat, WriteCodecType
from hare.time import Timezone
from hare.time.constants import DEFAULT_TIMEZONE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.query.rows.native.hydration_layout import HydrationEntry

#: A codec type and the options it is built with.
type CodecSpecification[CodecType] = tuple[CodecType, dict[str, Any]]


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
        """The configured zone and whether it is UTC, for a zone name - None without ``use_timezone``."""
        if zone_name is None:
            return None, False
        zone = Timezone.parse(zone_name)
        return zone, getattr(zone, "key", None) == DEFAULT_TIMEZONE

    @staticmethod
    def get_read_specification(
        entry: HydrationEntry, types: TypeRegistry, zone_name: str | None
    ) -> CodecSpecification[ReadCodecType]:
        """How a column is read.

        Args:
            entry: The column's hydration entry.
            types: The dialect's type registry.
            zone_name: The configured zone under ``use_timezone``, else None.

        Returns:
            The read codec type and its options.
        """
        _name, field, bucket, dialect_reader = entry
        use_timezone = zone_name is not None
        if dialect_reader is not None:
            if (
                isinstance(field, DatetimeField)
                and types.reads_naive_datetime_as_utc(type(field))
                and not FieldCodecs.overrides_read_conversion(field, DatetimeField)
            ):
                return FieldCodecs.get_datetime_read_specification(dialect_reader, zone_name, naive_is_utc=True)
            return ReadCodecType.CALL, {"reader": dialect_reader}
        if bucket == FieldBucket.NATIVE:
            return ReadCodecType.AS_IS, {}
        if bucket == FieldBucket.DEFAULT:
            return ReadCodecType.FIELD_TYPE, {"field_type": field.field_type}
        own_specification = field.get_read_codec_specification(types, zone_name)
        if own_specification is not None:
            return cast("CodecSpecification[ReadCodecType]", own_specification)
        fallback = field.from_db_value
        if isinstance(field, DatetimeField) and not FieldCodecs.overrides_read_conversion(field, DatetimeField):
            return FieldCodecs.get_datetime_read_specification(fallback, zone_name, naive_is_utc=False)
        if isinstance(field, TimeField) and not FieldCodecs.overrides_read_conversion(field, TimeField):
            return ReadCodecType.TIME, {
                "fallback": fallback,
                "use_timezone": use_timezone,
                # A fixed offset: a bare time has no date to resolve a named zone's offset against.
                "fixed_offset": Timezone.get_fixed_offset(zone_name) if use_timezone else None,
            }
        simple_types: tuple[tuple[type[Field[Any]], ReadCodecType], ...] = (
            (DateField, ReadCodecType.DATE),
            (TimeDeltaField, ReadCodecType.TIMEDELTA),
            (UUIDField, ReadCodecType.UUID),
            (BooleanField, ReadCodecType.BOOLEAN),
            (BinaryField, ReadCodecType.BINARY),
        )
        for field_class, codec_type in simple_types:
            if isinstance(field, field_class) and not FieldCodecs.overrides_read_conversion(field, field_class):
                return codec_type, {"fallback": fallback}
        for enum_field_class in (IntEnumFieldInstance, CharEnumFieldInstance):
            if isinstance(field, enum_field_class) and not FieldCodecs.overrides_read_conversion(
                field, enum_field_class
            ):
                return ReadCodecType.ENUMERATION, {
                    "members_by_value": cast("type[Enum]", field.enum_type)._value2member_map_,
                    "fallback": fallback,
                }
        if isinstance(field, DecimalField) and not FieldCodecs.overrides_read_conversion(field, DecimalField):
            return ReadCodecType.DECIMAL, {
                "quant": field.quant,
                "context": field.quantize_context,
                # Positional text with exactly this scale and no more digits than the precision is
                # already quantized - read as it is.
                "decimal_places": field.decimal_places,
                "precision": field.quantize_context.prec,
                "fallback": fallback,
            }
        if isinstance(field, JSONField) and not FieldCodecs.overrides_read_conversion(field, JSONField):
            type_adapter = field.get_value_type_adapter()
            return ReadCodecType.JSON, {
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
        return ReadCodecType.CALL, {"reader": fallback}

    @staticmethod
    def get_expression_read_specification(
        name: str, field: Field[Any], types: TypeRegistry, zone_name: str | None
    ) -> CodecSpecification[ReadCodecType]:
        """How a column computed by an expression - an annotation decoded through ``field`` - is read:
        always as the field reads it, since the driver may return another type than a column of the
        field holds.

        Args:
            name: The column's name.
            field: The field the value is decoded through.
            types: The dialect's type registry.
            zone_name: The configured zone under ``use_timezone``, else None.

        Returns:
            The read codec type and its options.
        """
        dialect_reader = types.get_python_reader(field) if types.get_python_converter(type(field)) else None
        codec_type, options = FieldCodecs.get_read_specification(
            (name, field, FieldBucket.COMPLEX, dialect_reader), types, zone_name
        )
        # A union of types (an int | str field) has no one exact type.
        field_type: Any = field.field_type
        keeps_exact_type = not FieldCodecs.overrides_read_conversion(field, Field) or (
            isinstance(field, IntField) and not FieldCodecs.overrides_read_conversion(field, IntField)
        )
        if (
            codec_type == ReadCodecType.CALL
            and dialect_reader is None
            and keeps_exact_type
            and isinstance(field_type, type)
        ):
            # Field.to_python and IntField's keep a value of exactly the field's type as it is.
            return ReadCodecType.EXACT_TYPE, {"exact_type": field_type, "reader": options["reader"]}
        return codec_type, options

    @staticmethod
    def get_datetime_read_specification(
        fallback: Callable[[Any], Any], zone_name: str | None, *, naive_is_utc: bool
    ) -> CodecSpecification[ReadCodecType]:
        """How a datetime column is read.

        Args:
            fallback: The reader of a value the codec leaves to Python.
            zone_name: The configured zone under ``use_timezone``, else None.
            naive_is_utc: A naive datetime from the driver is a UTC instant.

        Returns:
            The read codec type and its options.
        """
        zone, zone_is_utc = FieldCodecs.get_zone(zone_name)
        return ReadCodecType.DATETIME, {
            "fallback": fallback,
            "use_timezone": zone_name is not None,
            "zone": zone,
            "zone_is_utc": zone_is_utc,
            "naive_is_utc": naive_is_utc,
        }

    @staticmethod
    def get_inline_checks(field: Field[Any], codec_validator: Any = None) -> list[tuple[InlineCheckType, Any]] | None:
        """The field's validators as checks the codec makes itself, in their order.

        Args:
            field: The field.
            codec_validator: A validator the codec checks by its own code - left out.

        Returns:
            The checks; None when a validator is of another type or has a message of its own, and
            ``field.validate()`` checks the value.
        """
        checks: list[tuple[InlineCheckType, Any]] = []
        is_number = field.field_type in INLINE_VALUE_CHECK_TYPES
        for validator in field.validators:
            if validator is codec_validator:
                continue
            # A subclass may check otherwise.
            if type(validator) not in INLINE_CHECK_VALIDATOR_TYPES or getattr(validator, "message", None) is not None:
                return None
            if isinstance(validator, MinValueValidator) and is_number:
                checks.append((InlineCheckType.MIN_VALUE, validator.min_value))
            elif isinstance(validator, MaxValueValidator) and is_number:
                checks.append((InlineCheckType.MAX_VALUE, validator.max_value))
            elif isinstance(validator, MinLengthValidator) and field.field_type is str:
                checks.append((InlineCheckType.MIN_LENGTH, validator.min_length))
            elif isinstance(validator, MaxLengthValidator) and field.field_type is str:
                checks.append((InlineCheckType.MAX_LENGTH, validator.max_length))
            elif (
                isinstance(validator, MaxDigitsValidator)
                and isinstance(field, DecimalField)
                and (validator.max_digits, validator.decimal_places) == (field.max_digits, field.decimal_places)
            ):
                checks.append((InlineCheckType.MAX_DIGITS, (field.max_digits, field.decimal_places)))
            else:
                return None
        return checks

    @staticmethod
    def get_validation_options(field: Field[Any], codec_validator: Any = None) -> dict[str, Any]:
        """The options a codec checks a written value with: the inline checks, or ``field.validate``.

        Args:
            field: The field.
            codec_validator: A validator the codec checks by its own code.

        Returns:
            The options.
        """
        checks = FieldCodecs.get_inline_checks(field, codec_validator)
        if checks is None:
            return {"validate": field.validate}
        return {"checks": checks}

    @staticmethod
    def get_text_format_options(field: Field[Any]) -> dict[str, Any] | None:
        """The options of the text format codec writing a field of a format - an ``EmailField``,
        ``URLField``, ``SlugField``, or a ``PhoneField`` without ``phonenumbers``.

        Args:
            field: The field.

        Returns:
            The format and its options; None when the field isn't one of them, converts values its
            own way, or its format validator was replaced or given a message of its own.
        """
        format_validator = getattr(field, "format_validator", None)
        if (
            format_validator is None
            or format_validator not in field.validators
            or getattr(format_validator, "message", None) is not None
        ):
            return None
        if (
            isinstance(field, EmailField)
            and not FieldCodecs.overrides_write_conversion(field, EmailField)
            and type(field).get_normalized_address is EmailField.get_normalized_address
            and type(format_validator) is EmailValidator
            and not format_validator.allowed_domains
        ):
            return {"format": TextFormat.EMAIL, "lowercase": field.lowercase}
        if (
            isinstance(field, URLField)
            and not FieldCodecs.overrides_write_conversion(field, URLField)
            and type(format_validator) is URLValidator
            and list(format_validator.allowed_schemes) == list(field.schemes)
        ):
            return {"format": TextFormat.URL, "schemes": list(field.schemes)}
        if (
            isinstance(field, SlugField)
            and not FieldCodecs.overrides_write_conversion(field, SlugField)
            and type(format_validator) is SlugValidator
            and format_validator.allow_unicode is field.allow_unicode
        ):
            # An ASCII slug is one with or without allow_unicode - the field decides on any other.
            return {"format": TextFormat.SLUG}
        if (
            isinstance(field, PhoneField)
            and not FieldCodecs.overrides_write_conversion(field, PhoneField)
            and type(format_validator) is E164PhoneValidator
            and field.phonenumbers is None
        ):
            return {"format": TextFormat.PHONE}
        return None

    @staticmethod
    def get_write_specification(
        field: Field[Any], types: TypeRegistry, zone_name: str | None, *, uses_field_conversion: bool = False
    ) -> CodecSpecification[WriteCodecType]:
        """How a value of a field is written.

        Args:
            field: The field.
            types: The dialect's type registry.
            zone_name: The configured zone under ``use_timezone``, else None.
            uses_field_conversion: Whether the value is converted by the field's own
                ``to_db_value()`` - a filter's value is - and not by the one the dialect registers
                for written values.

        Returns:
            The write codec type and its options.
        """
        if uses_field_conversion:
            writer: Callable[[Any, Any], Any] = field.to_db_value
            converter: Callable[..., Any] | None = None
        else:
            writer = types.get_db_writer(field)
            converter = types.get_db_converter(type(field))
        if field.sensitive:
            # The field's own messages hide a sensitive value.
            return WriteCodecType.CALL, {"writer": writer}
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
            # use_timezone, which the codec leaves to the writer.
            zone, zone_is_utc = FieldCodecs.get_zone(zone_name)
            return WriteCodecType.DATETIME, {
                **common,
                "use_timezone": zone_name is not None,
                "zone": zone,
                "zone_is_utc": zone_is_utc,
                "stores_utc_instants": converter is DatetimeField.to_db_instant_value,
                "text": DatetimeField.field_type in types.bound_as_text,
                "auto_now": field.auto_now,
                "auto_now_add": field.auto_now_add,
                "assign_directly": FieldCodecs.is_plain_attribute(field),
                "validate": validate,
            }
        if converter is UUIDField.to_db_uuid:
            # The UUID itself, for a driver binding one - as to_db_uuid() writes it.
            return WriteCodecType.UUID, {**common, "validate": validate, "as_object": True}
        if converter is not None:
            return WriteCodecType.CALL, {"writer": writer}
        if isinstance(field, DateField) and not FieldCodecs.overrides_write_conversion(field, DateField):
            return WriteCodecType.DATE, {
                **common,
                "text": DateField.field_type in types.bound_as_text,
                "validate": validate,
            }
        if (
            isinstance(field, TimeField)
            and not FieldCodecs.overrides_write_conversion(field, TimeField)
            and not (field.auto_now or field.auto_now_add)
        ):
            return WriteCodecType.TIME, {
                **common,
                "use_timezone": zone_name is not None,
                "text": TimeField.field_type in types.bound_as_text,
                "validate": validate,
            }
        if isinstance(field, TimeDeltaField) and not FieldCodecs.overrides_write_conversion(field, TimeDeltaField):
            return WriteCodecType.TIMEDELTA, {**common, "validate": validate}
        if isinstance(field, UUIDField) and not FieldCodecs.overrides_write_conversion(field, UUIDField):
            return WriteCodecType.UUID, {**common, "validate": validate}
        if isinstance(field, IntEnumFieldInstance) and not FieldCodecs.overrides_write_conversion(
            field, IntEnumFieldInstance
        ):
            return WriteCodecType.ENUMERATION, {
                **common,
                "enum_type": field.enum_type,
                "stored_values_by_member": {member: int(member.value) for member in field.enum_type},
                "validate": validate,
            }
        if isinstance(field, CharEnumFieldInstance) and not FieldCodecs.overrides_write_conversion(
            field, CharEnumFieldInstance
        ):
            return WriteCodecType.ENUMERATION, {
                **common,
                "enum_type": field.enum_type,
                "stored_values_by_member": {member: str(member.value) for member in field.enum_type},
                "validate": validate,
            }
        if isinstance(field, DecimalField) and not FieldCodecs.overrides_write_conversion(field, DecimalField):
            return WriteCodecType.DECIMAL, {
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
            return WriteCodecType.JSON, {**common, "validate": validate}
        text_format_options = FieldCodecs.get_text_format_options(field)
        if text_format_options is not None:
            return WriteCodecType.TEXT_FORMAT, {
                **common,
                **text_format_options,
                **FieldCodecs.get_validation_options(field, getattr(field, "format_validator", None)),
            }
        if isinstance(field, BinaryField) and not FieldCodecs.overrides_write_conversion(field, BinaryField):
            return WriteCodecType.SCALAR, {
                **common,
                "exact_type": bytes,
                **FieldCodecs.get_validation_options(field),
            }
        write_check = field.get_native_write_check()
        if isinstance(field.field_type, type) and (
            write_check is not None or not FieldCodecs.overrides_write_conversion(field, Field)
        ):
            return WriteCodecType.SCALAR, {
                **common,
                "exact_type": field.field_type,
                "check": int(write_check) if write_check is not None else 0,
                **FieldCodecs.get_validation_options(field),
            }
        return WriteCodecType.CALL, {"writer": writer}
