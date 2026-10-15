from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, DecryptionError
from hare.fields.data.json.json_codec import JsonCodec
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.constants import (
    ENCRYPTED_JSON_FIELD_ENCRYPTED_KEYS_SUPPORTED_LOOKUPS,
    ENCRYPTED_JSON_FIELD_SUPPORTED_LOOKUPS,
)
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.lazy_loading.pydantic_classes import PydanticClasses

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model


class EncryptedJSONField(EncryptedFieldBase, JSONField[Any]):
    """A JSON column whose every value is stored Fernet-encrypted - at any depth of lists and dicts,
    whatever its type: each string, number, boolean and null is stored as a token of its JSON text
    and read back as the same value. The document's shape - its nesting and list lengths - stays
    visible, and so do the dict keys unless ``encrypt_keys`` is set.

    The value is serialized through ``encoder``/``decoder`` first, so a value the encoder writes
    as a JSON string (a ``datetime``, ``date``, ``UUID``, ...) is stored as that string. A
    ``field_type`` (e.g. a pydantic model) is validated on write and restored after decryption on
    read. Like ``JSONField``, an assigned ``str`` is a value, not JSON text.

    Needs ``hare.fields.encrypted.field_encryption.FieldEncryption.configure()`` and the ``encryption`` extra.
    Supported lookups: ``__isnull``/``__not_isnull``, and with plaintext keys also
    ``__has_key``/``__has_keys``/``__has_any_keys``.

    Args:
        encrypt_keys: Store every dict key - at any depth - as a Fernet token too.
    """

    def __init__(self, *, encrypt_keys: bool = False, **kwargs: Any) -> None:
        if not isinstance(encrypt_keys, bool):
            raise ConfigurationError(f"EncryptedJSONField(encrypt_keys=...) takes a bool, got {encrypt_keys!r}")
        super().__init__(**kwargs)
        self.encrypt_keys = encrypt_keys
        self.supported_lookups = (
            ENCRYPTED_JSON_FIELD_ENCRYPTED_KEYS_SUPPORTED_LOOKUPS
            if encrypt_keys
            else ENCRYPTED_JSON_FIELD_SUPPORTED_LOOKUPS
        )

    def get_unsupported_lookup_message(self) -> str:
        if not self.encrypt_keys:
            return super().get_unsupported_lookup_message()
        return (
            f"{self.get_field_label()} is encrypted with non-deterministic (Fernet) encryption, its keys "
            "included (encrypt_keys=True) - filtering by a value or a key can never match a stored row. "
            "Supported lookups: __isnull, __not_isnull."
        )

    def get_plain_value(self, value: Any) -> Any:
        """Converts ``value`` into the plain JSON value whose every leaf gets encrypted - each value
        the encoder writes as a JSON string is a ``str`` in the result.

        Args:
            value: Any JSON value, or a value of the declared ``field_type``.

        Returns:
            The JSON value - a dict, list, str, number, bool or None.

        Raises:
            ValidationError: ``value`` doesn't match ``field_type``, can't be stored or isn't JSON
                serializable.
        """
        type_adapter = self.get_value_type_adapter()
        if type_adapter is not None:
            typed_value = self.validate_declared_type(value)
            self.check_storable_value(type_adapter.dump_python(typed_value))
            plain_value = type_adapter.dump_python(typed_value, mode="json")
            self.check_storable_value(plain_value)
            return plain_value
        if PydanticClasses.is_model_instance(value):
            plain_value = value.model_dump(mode="json")
            self.check_storable_value(plain_value)
            return plain_value
        return self.decoder(self.encode_value(value))

    def encrypt_document(self, value: Any) -> Any:
        """Encrypts every leaf of a plain JSON value - and every dict key with ``encrypt_keys``.

        Args:
            value: The plain JSON value.

        Returns:
            The value with each leaf - and key - a Fernet token.
        """
        if isinstance(value, dict):
            encrypt_keys = self.encrypt_keys
            return {
                (FieldEncryption.encrypt(key) if encrypt_keys else key): self.encrypt_document(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.encrypt_document(item) for item in value]
        return FieldEncryption.encrypt(JsonCodec.dumps_exact(value))

    def decrypt_document(self, value: Any, field_label: str) -> Any:
        """Decrypts every leaf of a stored document - and every dict key with ``encrypt_keys``.

        Args:
            value: The stored document, decoded from its JSON text.
            field_label: ``Model.field`` for the error message.

        Returns:
            The plain JSON value.

        Raises:
            DecryptionError: A leaf or key isn't a token of the configured keys, or a leaf isn't
                a token at all.
        """
        if isinstance(value, dict):
            encrypt_keys = self.encrypt_keys
            return {
                (FieldEncryption.decrypt(key, field_label) if encrypt_keys else key): self.decrypt_document(
                    item, field_label
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.decrypt_document(item, field_label) for item in value]
        if not isinstance(value, str):
            raise DecryptionError(
                f"{field_label}: stored value can't be decrypted - it holds a plain {type(value).__name__} where "
                "every value is an encrypted token."
            )
        plaintext = FieldEncryption.decrypt(value, field_label)
        try:
            return JsonCodec.loads(plaintext)
        except ValueError as error:
            raise DecryptionError(
                f"{field_label}: stored value can't be decrypted - a token holds no JSON value."
            ) from error

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> str | None:
        self.validate(value)
        if value is None:
            return None
        return JsonCodec.dumps(self.encrypt_document(self.get_plain_value(value)))

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        stored_document = self.decode_stored_document(value)
        return self.validate_declared_type(self.decrypt_document(stored_document, self.get_field_label()))

    def decode_stored_document(self, value: Any) -> Any:
        """Decodes the column's JSON text - a driver that decodes JSON itself hands a dict or list.

        Args:
            value: The stored value.

        Returns:
            The stored document.

        Raises:
            ValidationError: The text isn't valid JSON.
        """
        if not isinstance(value, (str, bytes)):
            return value
        validation_error = None
        try:
            return self.decoder(value)
        except Exception as error:
            validation_error = self.get_validation_error(
                error, value, f"{self.get_field_label()}: stored value is not valid JSON."
            )
        raise validation_error

    def get_stored_value_conversion(self, old_field: Field[Any]) -> Callable[[Any], Any] | None:
        """Rewrites the keys of every stored document when ``encrypt_keys`` changes - the leaves
        are tokens either way and stay as they are.

        Args:
            old_field: The field's definition the stored values were written with.

        Returns:
            The conversion of one stored value, None when ``encrypt_keys`` doesn't change.
        """
        if not isinstance(old_field, EncryptedJSONField) or old_field.encrypt_keys == self.encrypt_keys:
            return None
        return self.encrypt_stored_keys if self.encrypt_keys else self.decrypt_stored_keys

    def encrypt_stored_keys(self, stored_value: Any) -> str:
        """A document stored with plaintext keys, its keys encrypted.

        Args:
            stored_value: The stored value.

        Returns:
            The JSON text to store.
        """
        return JsonCodec.dumps(
            self.get_document_with_keys(self.decode_stored_document(stored_value), FieldEncryption.encrypt)
        )

    def decrypt_stored_keys(self, stored_value: Any) -> str:
        """A document stored with encrypted keys, its keys decrypted.

        Args:
            stored_value: The stored value.

        Returns:
            The JSON text to store.

        Raises:
            DecryptionError: A key isn't a token of the configured keys.
        """
        field_label = self.get_field_label()
        return JsonCodec.dumps(
            self.get_document_with_keys(
                self.decode_stored_document(stored_value), lambda key: FieldEncryption.decrypt(key, field_label)
            )
        )

    @classmethod
    def get_document_with_keys(cls, document: Any, convert_key: Callable[[str], str]) -> Any:
        """A stored document with every dict key - at any depth - converted, its leaves as they are.

        Args:
            document: The stored document.
            convert_key: Converts one key.

        Returns:
            The document.
        """
        if isinstance(document, dict):
            return {convert_key(key): cls.get_document_with_keys(item, convert_key) for key, item in document.items()}
        if isinstance(document, list):
            return [cls.get_document_with_keys(item, convert_key) for item in document]
        return document
