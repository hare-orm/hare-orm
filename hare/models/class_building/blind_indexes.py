from __future__ import annotations

from typing import Any

from hare.exceptions import ConfigurationError
from hare.fields.constants import BLIND_INDEX_NAME_SUFFIX
from hare.fields.encrypted.blind_index_field import BlindIndexField
from hare.fields.encrypted.encrypted_text_field import EncryptedTextField


class BlindIndexes:
    """Adds the blind index field of every ``EncryptedTextField(blind_index=True)`` to a model being
    built."""

    @staticmethod
    def expand_declarations(attributes: dict[str, Any], model_name: str) -> dict[str, Any]:
        """Puts the blind index of every encrypted field declared with one right after it - unless the
        attributes already have it (an inherited one, a model rebuilt from a migration).

        Args:
            attributes: The class attributes - own and inherited.
            model_name: The model's name, for an error.

        Returns:
            The attributes with the blind indexes.

        Raises:
            ConfigurationError: The blind index's name is another attribute's.
        """
        if not any(isinstance(value, EncryptedTextField) and value.blind_index for value in attributes.values()):
            return attributes
        expanded_attributes: dict[str, Any] = {}
        for name, value in attributes.items():
            expanded_attributes[name] = value
            if not (isinstance(value, EncryptedTextField) and value.blind_index):
                continue
            blind_index_name = f"{name}{BLIND_INDEX_NAME_SUFFIX}"
            existing = attributes.get(blind_index_name)
            if existing is not None:
                if not (isinstance(existing, BlindIndexField) and existing.blind_index_source_field_name == name):
                    raise ConfigurationError(
                        f"{model_name}.{name}: its blind index {blind_index_name!r} is the name of another "
                        f"attribute of {model_name}"
                    )
                continue
            column = f"{value.source_field or name}{BLIND_INDEX_NAME_SUFFIX}"
            expanded_attributes[blind_index_name] = BlindIndexField(
                name,
                source_field=column if value.source_field else None,
                unique=value.blind_index_unique,
                db_index=not value.blind_index_unique,
                null=True,
            )
        return expanded_attributes
