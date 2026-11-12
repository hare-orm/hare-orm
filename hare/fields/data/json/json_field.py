from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from typing_extensions import TypeVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.constants import JSON_FIELD_TYPE_PACKAGE_MESSAGE, SENSITIVE_VALUE_PLACEHOLDER
from hare.fields.data.constants import (
    JSON_CIRCULAR_REFERENCE_MESSAGE,
    JSON_CIRCULAR_REFERENCE_PROBLEM,
    JSON_LONG_INTEGER_PROBLEM,
    JSON_MAXIMUM_NESTING,
    JSON_NESTED_TOO_DEEPLY_MESSAGE,
    JSON_NESTED_TOO_DEEPLY_PROBLEM,
    JSON_NON_FINITE_FLOAT_PROBLEM,
    JSON_PLAIN_PYTHON_VALUE_TYPES,
    JSON_VALUE_TYPE,
)
from hare.fields.data.json.json_codec import JsonCodec
from hare.fields.field import Field
from hare.fields.json_values import JsonValues
from hare.lazy_loading.pydantic_classes import PydanticClasses
from hare.native.native_modules import NativeModules

if TYPE_CHECKING:
    from hare.fields.data.json.json_path_field import JSONPathField
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup

T = TypeVar("T", default=Any)


# The signatures of the JSON encoder and decoder a field takes (``encoder``, ``decoder``).
JsonDumpsFunction = Callable[[Any], str]


JsonLoadsFunction = Callable[[str | bytes], Any]


class JSONField(Field[T]):
    """A field holding a JSON value - ``JSONField[dict[str, str]]`` for type checkers.

    The default codec uses the required orjson dependency and preserves integers beyond its range.

    Args:
        encoder: The JSON encoder.
        decoder: The JSON decoder.
        field_type: A declared type accepted by pydantic TypeAdapter, used to validate and restore values.
    """

    field_type = dict[str, Any] | list[Any]

    SQL_TYPE = "JSON"
    indexable = False
    holds_container_value = True
    argument_value_type = JSON_VALUE_TYPE
    #: Whether the field is kept as its JSON text where a database's JSON type keeps less than the
    #: text does - every JSON value read back as written.
    keeps_json_text: bool = False

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

    def get_json_value(self, value: Any) -> Any:
        """The JSON form of a value of the field - a value of a declared ``field_type`` as pydantic writes it as JSON,
        any other one as ``JsonValues`` does.

        Args:
            value: A value of the field, None included.

        Returns:
            The value's JSON form.

        Raises:
            TypeError: A value inside it has no JSON form.
        """
        type_adapter = self.get_value_type_adapter()
        if type_adapter is not None and value is not None:
            return type_adapter.dump_python(value, mode="json")
        return JsonValues.get_json_value(value)

    def _get_constructor_argument_value(self, name: str) -> Any:
        # The class's own field_type is the value type of an undeclared document, not an argument.
        if name == "field_type":
            return self.declared_value_type
        return super()._get_constructor_argument_value(name)

    def get_value_type_adapter(self) -> Any:
        """The pydantic TypeAdapter for ``declared_value_type``, None without a declared type.

        Raises:
            ConfigurationError: A type is declared and pydantic isn't installed - its values would go
                unvalidated.
        """
        if not self.declared_value_type:
            return None
        if self._value_type_adapter is None:
            type_adapter = PydanticClasses.get_type_adapter()
            if type_adapter is None:
                raise ConfigurationError(JSON_FIELD_TYPE_PACKAGE_MESSAGE)
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

    @staticmethod
    def get_nesting_message(value: Any) -> str | None:
        """What is wrong with how a JSON value nests its lists, tuples and dicts, however deep - a
        container holding itself is told apart first, as it nests without end.

        Args:
            value: A decoded JSON value.

        Returns:
            The message of a circular reference or of nesting past ``JSON_MAXIMUM_NESTING``; None
            for a value nesting fine.
        """
        # The containers on the path to the item, outermost first, each with the stack's length
        # when it was entered: its children are the stack's entries from that length on, so one
        # entered at a longer stack has been left.
        open_containers: list[tuple[Any, int]] = []
        pending_values = [value]
        while pending_values:
            item = pending_values.pop()
            if not isinstance(item, (dict, list, tuple)):
                continue
            pending_length = len(pending_values)
            while open_containers and open_containers[-1][1] > pending_length:
                open_containers.pop()
            if any(container is item for container, _entered_length in open_containers):
                return JSON_CIRCULAR_REFERENCE_MESSAGE
            if len(open_containers) >= JSON_MAXIMUM_NESTING:
                return JSON_NESTED_TOO_DEEPLY_MESSAGE
            open_containers.append((item, pending_length))
            pending_values.extend(item.values() if isinstance(item, dict) else item)
        return None

    def raise_if_nested_wrongly(self, value: Any) -> None:
        """Refuses a JSON value holding itself or nesting containers too deeply.

        Args:
            value: The value.

        Raises:
            ValidationError: ``get_nesting_message()`` names a problem.
        """
        nesting_message = self.get_nesting_message(value)
        if nesting_message is not None:
            raise ValidationError(f"{self.get_field_label()}: {nesting_message}")

    def check_storable_value(self, value: Any) -> bool:
        """Checks finite numbers, cycles and nesting depth before encoding, and identifies integers
        needing the exact encoder. This rejects NaN/Infinity before orjson can turn them into ``null``.

        Args:
            value: A decoded JSON value - dict, list, str, or any other JSON-scalar type.

        Returns:
            Whether ``value`` holds an int outside the range orjson can encode.

        Raises:
            ValidationError: A float is NaN/Infinity, a list or dict holds itself, or containers nest
                more than ``JSON_MAXIMUM_NESTING`` deep.
        """
        problem, non_finite_float = JsonCodec.storable_value_checker(value)
        if not problem:
            return False
        if problem == JSON_LONG_INTEGER_PROBLEM:
            return True
        if problem == JSON_CIRCULAR_REFERENCE_PROBLEM:
            raise ValidationError(f"{self.get_field_label()}: {JSON_CIRCULAR_REFERENCE_MESSAGE}")
        if problem == JSON_NESTED_TOO_DEEPLY_PROBLEM:
            raise ValidationError(f"{self.get_field_label()}: {JSON_NESTED_TOO_DEEPLY_MESSAGE}")
        if problem == JSON_NON_FINITE_FLOAT_PROBLEM:
            raise ValidationError(
                f"{self.get_field_label()}: value {self.get_value_for_message(non_finite_float)} is not a finite "
                "number, which JSON can't store"
            )
        return False

    def prepare_value(self, value: Any) -> tuple[Any, bool]:
        """Normalize and validate a document before its storage encoder runs.

        Declared types validate first. Their Python representation is checked before
        JSON serialization can replace a non-finite number with null. Custom encoders
        receive that Python representation, while the default encoder receives the
        JSON representation. Plain values, including strings and bytes, are passed
        to a custom encoder unchanged.

        Returns:
            The prepared value and whether it contains an integer requiring exact
            encoding beyond the optional encoder's integer range.

        Raises:
            ValidationError: The type, nesting or a numeric value is invalid.
        """
        if not self.declared_value_type and (
            type(value) in JSON_PLAIN_PYTHON_VALUE_TYPES or not PydanticClasses.is_model_instance(value)
        ):
            return value, self.check_storable_value(value)
        adapter = self.get_value_type_adapter()
        typed_value = self.validate_declared_type(value) if adapter is not None else value
        is_pydantic_model = adapter is None and PydanticClasses.is_model_instance(typed_value)
        if adapter is not None:
            python_value = adapter.dump_python(typed_value)
        elif is_pydantic_model:
            python_value = typed_value.model_dump()
        else:
            python_value = typed_value
        has_long_integer = self.check_storable_value(python_value)
        if self.encoder is JsonCodec.dumps:
            if adapter is not None:
                prepared = adapter.dump_python(typed_value, mode="json")
            elif is_pydantic_model:
                prepared = typed_value.model_dump(mode="json")
            else:
                prepared = python_value
            if prepared is not python_value:
                has_long_integer = self.check_storable_value(prepared)
        else:
            prepared = python_value
        return prepared, has_long_integer

    def encode_value(self, value: Any) -> str:
        """Prepare a non-null document, then encode it through the selected codec.

        Custom encoder exceptions are converted to the field's sensitive-safe error.
        """
        # Missing schema dependencies are configuration errors, before value handling.
        if self.declared_value_type:
            self.get_value_type_adapter()
        try:
            prepared, has_long_integer = self.prepare_value(value)
            return self.get_encoder(has_long_integer)(prepared)
        except ValidationError:
            raise
        except Exception as error:
            # A codec can reject deep containers before the Python fast walk
            # reaches its full nesting check. Keep the native and Python contract.
            self.raise_if_nested_wrongly(value)
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
        instance: type[Model] | Model | None,
    ) -> str | None:
        self.validate(value)
        return None if value is None else self.encode_value(value)

    def from_db_value(
        self, value: T | str | bytes | dict[str, Any] | list[Any] | None
    ) -> T | dict[str, Any] | list[Any] | None:
        if isinstance(value, (str, bytes)):
            validation_error = None
            try:
                data = self.decoder(value)
            except Exception as error:
                if isinstance(value, str):
                    shown = ValueDescriptions.get_shown_text(value)
                else:
                    try:
                        shown = ValueDescriptions.get_shown_text(value.decode())
                    except UnicodeDecodeError:
                        # repr() - decoding invalid UTF-8 would raise instead of the
                        # ValidationError.
                        shown = ValueDescriptions.get_shown_value(value)
                if self.sensitive:
                    shown = SENSITIVE_VALUE_PLACEHOLDER
                validation_error = self.get_validation_error(
                    error, value, f"{self.model_field_name}: value {shown} is invalid json value."
                )
            if validation_error is not None:
                raise validation_error
            value = data

        # A document of no declared type is read as decoded - validate_declared_type() keeps it so.
        if not self.declared_value_type:
            return value  # type: ignore[return-value]
        return self.validate_declared_type(value)

    def get_db_value_reader(self) -> Callable[[Any], Any]:
        # This class's from_db_value() with the default decoder and no declared type is read natively -
        # the decoder it calls, its error for a text that isn't JSON.
        if (
            type(self).from_db_value is JSONField.from_db_value
            and self.decoder is JsonCodec.loads
            and not self.declared_value_type
        ):
            return NativeModules.rows.JsonColumnReader(self.from_db_value)
        return super().get_db_value_reader()

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
            A ``JSONPathField`` sharing this field's encoder, decoder, sensitivity and name.
        """
        # Imported here: the modules import each other.
        from hare.fields.data.json.json_path_field import JSONPathField

        if self.path_value_field is None:
            self.path_value_field = JSONPathField(encoder=self.encoder, decoder=self.decoder, sensitive=self.sensitive)
            self.path_value_field.keeps_json_text = self.keeps_json_text
        self.path_value_field.model_field_name = self.model_field_name
        return self.path_value_field
