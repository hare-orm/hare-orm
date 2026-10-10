from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, FieldError
from hare.fields.constants import (
    DB_DEFAULT_NOT_SET,
)
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.expressions import Expression


class EncryptedFieldBase(Field[Any]):
    """Shared behavior of every Fernet-encrypted field: ``sensitive=True`` by default and no
    value-comparing lookups (Fernet tokens are non-deterministic, so a stored row can never be
    matched by value)."""

    encrypted = True

    def __init__(self, *, sensitive: bool = True, db_default: Any = DB_DEFAULT_NOT_SET, **kwargs: Any) -> None:
        if db_default is not DB_DEFAULT_NOT_SET:
            raise ConfigurationError(
                f"{self.__class__.__name__} can't take db_default= - a database-level default is written "
                "into the schema itself, so it would either be stored unencrypted or as one fixed token "
                "shared by every row (revealing which rows hold the default, and going stale once the "
                "key changes). Use a Python-side default= instead - it's encrypted on every save."
            )
        super().__init__(sensitive=sensitive, **kwargs)

    def get_field_label(self) -> str:
        """Returns ``Model.field`` for error messages."""
        model_name = getattr(self.model, "__name__", None)
        return f"{model_name}.{self.model_field_name}" if model_name else self.model_field_name

    def get_unsupported_lookup_message(self) -> str:
        supported = ", ".join(f"__{lookup}" for lookup in sorted(self.supported_lookups or ()))
        return (
            f"{self.get_field_label()} is encrypted with non-deterministic (Fernet) encryption - "
            f"filtering by its value can never match a stored row. Supported lookups: {supported}."
        )

    def get_unsupported_usage_message(self, usage: str) -> str:
        """Returns the error message for using this field in ``usage``.

        Args:
            usage: What the field is used in, e.g. ``"ORDER BY"``.

        Returns:
            The message.
        """
        return (
            f"{self.get_field_label()} is encrypted with non-deterministic (Fernet) encryption - "
            f"{usage} can't use it: the database only sees random ciphertext (the same value is stored "
            "differently in every row), so the result would be meaningless."
        )

    @staticmethod
    def raise_if_encrypted(field_object: Field[Any] | None, usage: str) -> None:
        """Rejects using an encrypted field in ``usage``.

        Args:
            field_object: The field being used, if known.
            usage: What the field is used in, e.g. ``"ORDER BY"``.

        Raises:
            FieldError: ``field_object`` is an encrypted field.
        """
        if isinstance(field_object, EncryptedFieldBase):
            raise FieldError(field_object.get_unsupported_usage_message(usage))

    @staticmethod
    def raise_if_compared_to_expression(
        model: type[Model], filter_key: str, expression_field: Field[Any] | None
    ) -> None:
        """Rejects a filter comparing an encrypted field with an expression (``F()``,
        ``Subquery``, ...), on either side - the database would compare ciphertext.

        Args:
            model: The filtered model.
            filter_key: The filter kwarg name, e.g. ``"owner__secret__gt"``.
            expression_field: The field the expression's value comes from, if known.

        Raises:
            FieldError: The filtered field or ``expression_field`` is encrypted.
        """
        # Local import: the models package imports the fields package.
        from hare.query.lookup_info.lookup_path import LookupPath

        lookup_path = LookupPath.parse(model, filter_key, crosses_last=True)
        field_object = lookup_path.model._meta.fields_map.get(lookup_path.field_name or "")
        if isinstance(field_object, EncryptedFieldBase):
            raise FieldError(field_object.get_unsupported_lookup_message())
        EncryptedFieldBase.raise_if_encrypted(expression_field, "a filter comparison")

    @staticmethod
    def raise_if_update_expression_invalid(
        target_field: Field[Any], expression: Expression, source_field: Field[Any] | None
    ) -> None:
        """Rejects an UPDATE expression that would write a non-ciphertext value into an encrypted
        column, or an encrypted column's ciphertext into a plain one.

        Args:
            target_field: The field being updated.
            expression: The expression assigned to it.
            source_field: The expression's own value field, if known.

        Raises:
            FieldError: The expression can't be assigned to ``target_field``.
        """
        from hare.query.expressions import F

        if isinstance(target_field, EncryptedFieldBase):
            if (
                type(expression) is F
                and "__" not in expression.name
                and source_field is not None
                and type(source_field) is type(target_field)
                and target_field.get_stored_value_conversion(source_field) is None
            ):
                return
            raise FieldError(
                f"{target_field.get_field_label()} is encrypted - it can only be updated to a plain value "
                f"(encrypted before it's written) or to F() of another {type(target_field).__name__} storing "
                "its values the same way (the same encrypt_keys). Any other SQL expression's result would "
                "be written unencrypted."
            )
        if isinstance(source_field, EncryptedFieldBase):
            raise FieldError(
                source_field.get_unsupported_usage_message(
                    f"updating {getattr(target_field.model, '__name__', '')}.{target_field.model_field_name}"
                )
            )

    def to_lookup_value(self, value: Any, instance: type[Model] | Model) -> Any:
        """Rejects filtering by value.

        Raises:
            FieldError: Always.
        """
        raise FieldError(self.get_unsupported_lookup_message())
