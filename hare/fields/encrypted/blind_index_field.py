from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.data.text.char_field import CharField
from hare.fields.encrypted.constants import BLIND_INDEX_LENGTH
from hare.fields.encrypted.field_encryption import FieldEncryption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class BlindIndexField(CharField[Any]):
    """The blind index of an ``EncryptedTextField(blind_index=True)`` - the HMAC of its plaintext,
    added to the model next to it. It is written whenever its field is, from the field's value, so
    an equality filter on the field compares indexes; the value assigned to it is never used.

    Args:
        blind_index_source_field_name: The encrypted field it indexes.
    """

    def __init__(self, blind_index_source_field_name: str, **kwargs: Any) -> None:
        kwargs.setdefault("null", True)
        kwargs.setdefault("sensitive", True)
        super().__init__(max_length=BLIND_INDEX_LENGTH, **kwargs)
        self.blind_index_source_field_name = blind_index_source_field_name

    def get_blind_index(self, plaintext: Any) -> str | None:
        """The index of a plaintext.

        Args:
            plaintext: The value, None for none.

        Returns:
            The index, None for None.
        """
        return None if plaintext is None else FieldEncryption.get_blind_index(str(plaintext))

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        # Written for an instance from its encrypted field's plaintext; for a model class (an
        # UPDATE, a filter) the value given is that plaintext.
        if hasattr(instance, "_saved_in_db"):
            blind_index = self.get_blind_index(getattr(instance, self.blind_index_source_field_name))
            # Kept on the instance as a read would give it.
            setattr(instance, self.model_field_name, blind_index)
            return blind_index
        return self.get_blind_index(value)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        kwargs.pop("max_length", None)
        return path, args, kwargs
