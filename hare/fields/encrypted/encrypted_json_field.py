from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.fields.constants import (
    ENCRYPTED_JSON_FIELD_SUPPORTED_LOOKUPS,
)
from hare.fields.data.json.json_field import JSONField
from hare.fields.encryption import FieldEncryption
from hare.utils.pydantic_classes import PydanticClasses

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin


class EncryptedJSONField(EncryptedFieldMixin, JSONField[dict[str, Any]]):  # type: ignore[misc]
    """A JSON column holding a dict whose own string values are stored Fernet-encrypted; keys and
    non-string values (numbers, booleans, nested lists/dicts) stay plain JSON.

    The value is serialized through ``encoder``/``decoder`` first, so a value the encoder writes
    as a JSON string (a ``datetime``, ``date``, ``UUID``, ...) is encrypted too. A ``field_type``
    (e.g. a pydantic model) is validated on write and restored after decryption on read.

    Needs ``hare.fields.encryption.configure_field_encryption()`` and the ``encryption`` extra. Only
    ``__isnull``/``__not_isnull``/``__has_key``/``__has_keys``/``__has_any_keys`` lookups are
    supported.
    """

    supported_lookups = ENCRYPTED_JSON_FIELD_SUPPORTED_LOOKUPS

    def get_dict_value(self, value: Any) -> dict[str, Any]:
        """Decodes ``value`` (a dict, or its JSON text) into a dict.

        Args:
            value: The value to decode.

        Returns:
            The dict.

        Raises:
            ValidationError: ``value`` isn't valid JSON, or isn't a JSON object.
        """
        if isinstance(value, (str, bytes)):
            validation_error = None
            try:
                value = self.decoder(value)
            except Exception as exc:
                validation_error = self.get_validation_error(
                    exc, value, f"{self.get_field_label()}: value is not valid JSON."
                )
            if validation_error is not None:
                raise validation_error
        if not isinstance(value, dict):
            raise ValidationError(
                f"{self.get_field_label()}: EncryptedJSONField expects a dict, got {type(value).__name__}"
            )
        return value

    def get_serialized_dict_value(self, value: Any) -> dict[str, Any]:
        """Converts ``value`` into the plain JSON dict that gets encrypted - every value the
        encoder writes as a JSON string is a ``str`` in the result.

        Args:
            value: A dict, its JSON text, or a value of the declared ``field_type``.

        Returns:
            The JSON dict.

        Raises:
            ValidationError: ``value`` doesn't match ``field_type``, isn't a JSON object or isn't
                JSON serializable.
        """
        type_adapter = self.get_value_type_adapter()
        if type_adapter is not None:
            return self.get_dict_value(type_adapter.dump_python(self.validate_declared_type(value), mode="json"))
        if PydanticClasses.is_model_instance(value):
            return self.get_dict_value(value.model_dump(mode="json"))
        if isinstance(value, (str, bytes)):
            return self.get_dict_value(value)
        dict_value = self.get_dict_value(value)
        encoder = self.get_encoder(self.check_storable_value(dict_value))
        try:
            return self.get_dict_value(self.decoder(encoder(dict_value)))
        except ValidationError:
            raise
        except Exception as exc:
            validation_error = self.get_validation_error(
                exc, value, f"{self.get_field_label()}: value is not JSON serializable."
            )
        raise validation_error

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> str | None:
        self.validate(value)
        if value is None:
            return None
        plain_value = self.get_serialized_dict_value(value)
        encoder = self.get_encoder(self.check_storable_value(plain_value))
        encrypted_value = {
            key: FieldEncryption.encrypt(item) if isinstance(item, str) and item else item
            for key, item in plain_value.items()
        }
        try:
            return encoder(encrypted_value)
        except Exception as exc:
            validation_error = self.get_validation_error(
                exc, value, f"{self.get_field_label()}: value is not JSON serializable."
            )
        raise validation_error

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        field_label = self.get_field_label()
        return self.validate_declared_type(
            {
                key: FieldEncryption.decrypt(item, field_label) if isinstance(item, str) and item else item
                for key, item in self.get_dict_value(value).items()
            }
        )
