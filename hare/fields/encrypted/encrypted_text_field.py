from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.fields.constants import (
    ENCRYPTED_TEXT_FIELD_SUPPORTED_LOOKUPS,
)
from hare.fields.data.text.text_field import TextField
from hare.fields.encryption import FieldEncryption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin


class EncryptedTextField(EncryptedFieldMixin, TextField):  # type: ignore[misc]
    """A ``TEXT`` column whose value is stored Fernet-encrypted.

    Needs ``hare.fields.encryption.configure_field_encryption()`` and the ``encryption`` extra. Only
    ``__isnull``/``__not_isnull`` lookups are supported.
    """

    supported_lookups = ENCRYPTED_TEXT_FIELD_SUPPORTED_LOOKUPS
    #: A Fernet token differs on every write, so an index/UNIQUE over it can never match a value.
    indexable = False

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> str | None:
        value = super().to_db_value(value, instance)
        if value is None:
            return None
        return FieldEncryption.encrypt(value)

    def from_db_value(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise ValidationError(f"{self.get_field_label()}: expected an encrypted token, got {type(value).__name__}")
        return FieldEncryption.decrypt(value, self.get_field_label())

    def to_python(self, value: Any) -> Any:
        # A freshly assigned value is plaintext - never decrypted, only coerced to str.
        if value is None or isinstance(value, str):
            return value
        return str(value)
