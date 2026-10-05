from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, FieldError
from hare.fields.constants import BLIND_INDEX_NAME_SUFFIX
from hare.fields.encrypted.blind_index_field import BlindIndexField
from hare.fields.encrypted.encrypted_text_field import EncryptedTextField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.meta_info import MetaInfo


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

    @staticmethod
    def expand_update_values(meta: MetaInfo, values: dict[str, Any]) -> dict[str, Any]:
        """The values of a ``QuerySet.update()`` with the blind index of every encrypted field
        updated - from the plaintext, or from ``F()`` of another field's blind index.

        Args:
            meta: The updated model's meta.
            values: The values by field name.

        Returns:
            The values with the blind indexes.

        Raises:
            FieldError: A blind index is given itself, or the field takes ``F()`` of a field
                without a blind index.
        """
        # Local import: the expressions import the models package.
        from hare.query.expressions import F

        expanded_values = dict(values)
        for field_name, blind_index_name in meta.blind_index_fields.items():
            if blind_index_name in values:
                raise FieldError(
                    f"{meta.full_name}.{blind_index_name} is a blind index - it is written from {field_name}"
                )
            if field_name not in values:
                continue
            value = values[field_name]
            if type(value) is F:
                source_blind_index_name = meta.blind_index_fields.get(value.name)
                if source_blind_index_name is None:
                    raise FieldError(
                        f"{meta.full_name}.{field_name} has a blind index - it can take F() only of a field "
                        "with one too"
                    )
                expanded_values[blind_index_name] = F(source_blind_index_name)
            else:
                # The plaintext - the blind index field computes its HMAC.
                expanded_values[blind_index_name] = value
        return expanded_values
