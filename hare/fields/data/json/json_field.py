from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.exceptions import ValidationError
from hare.fields.constants import NULL_BYTE_MESSAGE, SENSITIVE_VALUE_PLACEHOLDER
from hare.fields.data.constants import (
    JSON_LONG_INTEGER_PROBLEM,
    JSON_NON_FINITE_FLOAT_PROBLEM,
    JSON_NULL_BYTE_PROBLEM,
    ORJSON_INTEGER_MAX,
    ORJSON_INTEGER_MIN,
)
from hare.fields.data.json.json_codec import JsonCodec
from hare.fields.field import Field
from hare.lazy_loading.pydantic_classes import PydanticClasses

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.json.json_path_field import JSONPathField
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup

T = TypeVar("T")


# Doing this we can replace json dumps/loads with different implementations
JsonDumpsFunction = Callable[[Any], str]


JsonLoadsFunction = Callable[[str | bytes], Any]


class JSONField(Field[T]):
    """A JSON field holding a dict or list - ``JSONField[dict[str, str]]`` for type checkers. orjson is
    used when installed.

    Args:
        encoder: The JSON encoder.
        decoder: The JSON decoder.
        field_type: A pydantic model the value is validated into.
    """

    field_type = dict[str, Any] | list[Any]

    SQL_TYPE = "JSON"
    indexable = False
    holds_container_value = True

    def __init__(
        self,
        encoder: JsonDumpsFunction = JsonCodec.dumps,
        decoder: JsonLoadsFunction = JsonCodec.loads,
        field_type: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.encoder = encoder
        self.decoder = decoder
        self.path_value_field: JSONPathField | None = None
        #: The explicitly declared ``field_type`` (e.g. a pydantic model or ``list[Model]``), or
        #: None - values are validated/converted through a pydantic TypeAdapter for it.
        self.declared_value_type: Any = field_type
        self._value_type_adapter: Any = None
        if self.declared_value_type:
            self.field_type = self.declared_value_type

    def _get_constructor_argument_value(self, name: str) -> Any:
        # The class's own field_type is the value type of an undeclared document, not an argument.
        if name == "field_type":
            return self.declared_value_type
        return super()._get_constructor_argument_value(name)

    def get_value_type_adapter(self) -> Any:
        """The pydantic TypeAdapter for ``declared_value_type``, or None when there's no declared
        type or pydantic isn't installed."""
        if not self.declared_value_type:
            return None
        if self._value_type_adapter is None:
            type_adapter = PydanticClasses.get_type_adapter()
            if type_adapter is None:
                return None
            self._value_type_adapter = type_adapter(self.declared_value_type)
        return self._value_type_adapter

    def validate_declared_type(self, value: Any) -> Any:
        """Validates ``value`` into ``declared_value_type`` (a dict becomes the pydantic model,
        a list of dicts a list of models, ...); unchanged without a declared type.

        Raises:
            ValidationError: The value doesn't match the declared type.
        """
        type_adapter = self.get_value_type_adapter()
        if type_adapter is None or value is None:
            return value
        try:
            return type_adapter.validate_python(value)
        except Exception as error:
            shown_error = f"{SENSITIVE_VALUE_PLACEHOLDER} doesn't match the declared type" if self.sensitive else error
            validation_error = self.get_validation_error(error, value, f"{self.model_field_name}: {shown_error}")
        raise validation_error

    def get_field_label(self) -> str:
        """Returns the field name shown in an error message."""
        return self.model_field_name

    def check_storable_value(self, value: Any) -> bool:
        """Checks a decoded JSON value (however deeply nested) for anything a JSON column can't
        store - a null byte is always escaped away by the encoder (so it can't be spotted in the
        encoded text), and orjson silently writes NaN/Infinity as ``null``.

        Args:
            value: A decoded JSON value - dict, list, str, or any other JSON-scalar type.

        Returns:
            Whether ``value`` holds an int outside the range orjson can encode.

        Raises:
            ValidationError: A str value or dict key holds a null byte, or a float is NaN/Infinity.
        """
        storable_value_checker = JsonCodec.storable_value_checker
        if storable_value_checker is not None:
            problem, non_finite_float = storable_value_checker(value)
            if problem == JSON_NULL_BYTE_PROBLEM:
                raise ValidationError(f"{self.get_field_label()}: {NULL_BYTE_MESSAGE}")
            if problem == JSON_NON_FINITE_FLOAT_PROBLEM:
                raise ValidationError(
                    f"{self.get_field_label()}: value {self.get_value_for_message(non_finite_float)} is not a finite "
                    "number, which JSON can't store"
                )
            return problem == JSON_LONG_INTEGER_PROBLEM
        has_long_integer = False
        pending_values = [value]
        while pending_values:
            item = pending_values.pop()
            if isinstance(item, str):
                if "\x00" in item:
                    raise ValidationError(f"{self.get_field_label()}: {NULL_BYTE_MESSAGE}")
            elif isinstance(item, dict):
                pending_values.extend(item.keys())
                pending_values.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending_values.extend(item)
            elif isinstance(item, float):
                if not math.isfinite(item):
                    raise ValidationError(
                        f"{self.get_field_label()}: value {self.get_value_for_message(item)} is not a finite "
                        "number, which JSON can't store"
                    )
            elif isinstance(item, int) and not ORJSON_INTEGER_MIN <= item <= ORJSON_INTEGER_MAX:
                has_long_integer = True
        return has_long_integer

    def encode_value(self, value: Any) -> str:
        """Encodes a non-None value of a JSONField without a declared ``field_type`` into the text
        written to the column.

        Args:
            value: A dict, list, str, number or pydantic model.

        Returns:
            The JSON text.

        Raises:
            ValidationError: The value can't be stored or isn't JSON serializable.
        """
        is_pydantic_model = PydanticClasses.is_model_instance(value)
        native_encoder = JsonCodec.native_encoder
        if not is_pydantic_model and native_encoder is not None and self.encoder is JsonCodec.dumps:
            # The text orjson writes, checked on the same pass; any other value takes the paths below.
            text = native_encoder(value)
            if text is not None:
                return text
        storable_value_checker = JsonCodec.storable_value_checker
        if (
            not is_pydantic_model
            and storable_value_checker is not None
            and self.encoder is JsonCodec.dumps
            and JsonCodec.orjson is not None
        ):
            # The usual value under the default encoder: one native check, then orjson. Anything
            # the check or orjson objects to takes the full path below, which says what's wrong.
            problem, _non_finite_float = storable_value_checker(value)
            if not problem:
                try:
                    return JsonCodec.orjson.dumps(value).decode()
                except (TypeError, ValueError):
                    pass
        # SQLite round-trips a null byte inside JSON/TEXT byte-for-byte, but Postgres's raw text
        # protocol can't represent it in ANY text-based type (JSON/JSONB included) and rejects it
        # at INSERT time - checked upfront here, uniformly on both dialects.
        has_long_integer = self.check_storable_value(value.model_dump() if is_pydantic_model else value)

        if is_pydantic_model:
            if self.encoder is JsonCodec.dumps:
                return value.model_dump_json()
            # self.encoder may be a custom json encoder
            value = value.model_dump()

        try:
            return self.get_encoder(has_long_integer)(value)
        except Exception as error:
            validation_error = self.get_validation_error(
                error,
                value,
                f"{self.model_field_name}: value {self.get_value_for_message(value)} is not JSON serializable.",
            )
        raise validation_error

    def get_encoder(self, has_long_integer: bool) -> JsonDumpsFunction:
        """Returns the encoder for a value.

        Args:
            has_long_integer: The value holds an int outside the range orjson can encode.

        Returns:
            ``encoder``, or the standard-library one for a long int under the default encoder.
        """
        return JsonCodec.dumps_exact if has_long_integer and self.encoder is JsonCodec.dumps else self.encoder

    def to_db_value(
        self,
        value: T | dict[str, Any] | list[Any] | str | bytes | None,
        instance: type[Model] | Model,
    ) -> str | None:
        self.validate(value)
        if value is None:
            return None

        type_adapter = self.get_value_type_adapter()
        if type_adapter is None:
            return self.encode_value(value)

        # Checked against field_type before writing. The JSON-mode dump is written; the python-mode
        # one still holds the NaN/Infinity floats.
        typed_value = self.validate_declared_type(value)
        self.check_storable_value(type_adapter.dump_python(typed_value, mode="json"))
        self.check_storable_value(type_adapter.dump_python(typed_value))
        try:
            if self.encoder is JsonCodec.dumps:
                return cast("str", type_adapter.dump_json(typed_value).decode())
            return self.encoder(type_adapter.dump_python(typed_value))
        except Exception as error:
            validation_error = self.get_validation_error(
                error,
                value,
                f"{self.model_field_name}: value {self.get_value_for_message(value)} is not JSON serializable.",
            )
        raise validation_error

    def from_db_value(
        self, value: T | str | bytes | dict[str, Any] | list[Any] | None
    ) -> T | dict[str, Any] | list[Any] | None:
        if isinstance(value, (str, bytes)):
            validation_error = None
            try:
                data = self.decoder(value)
            except Exception as error:
                if isinstance(value, str):
                    shown = value
                else:
                    try:
                        shown = value.decode()
                    except UnicodeDecodeError:
                        # repr() - decoding invalid UTF-8 would raise instead of the
                        # ValidationError.
                        shown = repr(value)
                if self.sensitive:
                    shown = SENSITIVE_VALUE_PLACEHOLDER
                validation_error = self.get_validation_error(
                    error, value, f"{self.model_field_name}: value {shown} is invalid json value."
                )
            if validation_error is not None:
                raise validation_error

            return self.validate_declared_type(data)

        return self.validate_declared_type(value)

    def get_assign_normalized_types(self) -> frozenset[type]:
        if self.declared_value_type is not None:
            return frozenset()
        return frozenset({dict, list, str, bytes, int, float, bool})

    def to_python(
        self, value: T | dict[str, Any] | list[Any] | str | bytes | None
    ) -> T | dict[str, Any] | list[Any] | str | bytes | None:
        # An assigned str/bytes is a value to store, not JSON text to decode; validated into
        # field_type when declared.
        return self.validate_declared_type(value)

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The lookups of a JSON value - containment, keys, paths."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.lookups.field_lookups import FieldLookups

        return FieldLookups.get_json(self)

    def get_path_value_field(self) -> JSONPathField:
        """The output field of a JSON path into this field (``F("data__key")``), created once.

        Returns:
            A ``JSONPathField`` sharing this field's encoder, decoder and name.
        """
        # Imported here: the modules import each other.
        from hare.fields.data.json.json_path_field import JSONPathField

        if self.path_value_field is None:
            self.path_value_field = JSONPathField(encoder=self.encoder, decoder=self.decoder)
        self.path_value_field.model_field_name = self.model_field_name
        return self.path_value_field
