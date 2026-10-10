from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, FieldError, ValidationError
from hare.fields.constants import BLIND_INDEX_NAME_SUFFIX
from hare.fields.data.text.text_field import TextField
from hare.fields.encrypted.constants import (
    BLIND_INDEX_COMPARED_LOOKUPS,
    BLIND_INDEX_SUPPORTED_LOOKUPS,
    ENCRYPTED_TEXT_FIELD_SUPPORTED_LOOKUPS,
)
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.sql.terms.field import Field as ColumnTerm

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Callable

    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase


class EncryptedTextField(EncryptedFieldBase, TextField):
    """A ``TEXT`` column whose value is stored Fernet-encrypted.

    Needs ``hare.fields.encrypted.field_encryption.FieldEncryption.configure()`` and the ``encryption`` extra. Only
    ``__isnull``/``__not_isnull`` lookups are supported - unless ``blind_index=True``: the model then
    gets a ``<name>_blind_index`` column holding the HMAC of the plaintext, written with the field,
    and equality and membership (``=``, ``__not``, ``__in``, ``__not_in``) compare it. With a blind
    index, ``unique=True`` makes the index unique.

    Args:
        blind_index: Whether the field has a blind index.
        unique: Whether no two rows hold the same value - needs ``blind_index=True``.

    Raises:
        ConfigurationError: ``blind_index`` isn't a bool, or ``unique=True`` without it.
    """

    supported_lookups = ENCRYPTED_TEXT_FIELD_SUPPORTED_LOOKUPS
    #: A Fernet token differs on every write, so an index/UNIQUE over it can never match a value.
    indexable = False

    def __init__(self, *, blind_index: bool = False, unique: bool = False, **kwargs: Any) -> None:
        if type(blind_index) is not bool:
            raise ConfigurationError(f"EncryptedTextField: blind_index must be a bool, got {blind_index!r}")
        super().__init__(unique=unique and not blind_index, **kwargs)
        self.blind_index = blind_index
        #: Whether the blind index is unique - the field's own ``unique``.
        self.blind_index_unique = blind_index and unique
        if blind_index:
            self.supported_lookups = BLIND_INDEX_SUPPORTED_LOOKUPS

    def get_blind_index_column(self) -> str:
        """The column of the field's blind index."""
        return f"{self.source_field or self.model_field_name}{BLIND_INDEX_NAME_SUFFIX}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.blind_index_unique:
            kwargs["unique"] = True
        return path, args, kwargs

    def get_lookups(self) -> dict[str, FieldLookup]:
        lookups = super().get_lookups()
        if not self.blind_index:
            return lookups
        # Local import: the filters package imports the fields package.
        from hare.query.filters.lookups.field_lookups import FieldLookups

        generic = FieldLookups.get_generic(self)
        for lookup_name in BLIND_INDEX_COMPARED_LOOKUPS:
            generic_lookup = generic[lookup_name]
            lookups[lookup_name] = generic_lookup.with_changes(
                operator=partial(
                    EncryptedTextField.compare_blind_index, generic_lookup.operator, self.get_blind_index_column()
                )
            )
        return lookups

    @staticmethod
    def compare_blind_index(operator: Callable[..., Any], column: str, term: Term, value: Any) -> Any:
        """A lookup of the field run on its blind index column instead.

        Args:
            operator: The lookup's own criterion builder.
            column: The blind index column.
            term: The field's column.
            value: The encoded value - the blind index of the plaintext.

        Returns:
            The criterion.

        Raises:
            FieldError: The field is read through an expression, not its column.
        """
        if not isinstance(term, ColumnTerm):
            raise FieldError("A blind index compares the field's own column, not an expression over it")
        return operator(ColumnTerm(column, table=term.table), value)

    def to_lookup_value(self, value: Any, instance: type[Model] | Model) -> Any:
        if not self.blind_index:
            return super().to_lookup_value(value, instance)
        return None if value is None else FieldEncryption.get_blind_index(str(value))

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
