from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, overload

from hare.exceptions import ConfigurationError
from hare.fields.data.constants import EMAIL_FIELD_MAX_LENGTH
from hare.fields.data.text.char_field import CharField, TStr
from hare.fields.validators.formats.email_validator import EmailValidator

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class EmailField(CharField[TStr]):
    """An email address, checked by ``EmailValidator``. The domain is written lowercase - the whole
    address with ``lowercase`` - on every write and in filter values, so ``Ann@Example.COM`` is
    stored and found as ``Ann@example.com``.

    Args:
        max_length: The most characters the address may have - 254 by default, the longest
            address a mail path holds.
        lowercase: Write the local part lowercase too. Off by default: a mail server may tell
            ``Ann`` and ``ann`` apart.
    """

    @overload
    def __init__(
        self: EmailField[str],
        max_length: int = EMAIL_FIELD_MAX_LENGTH,
        *,
        lowercase: bool = False,
        null: Literal[False] = False,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self: EmailField[str | None],
        max_length: int = EMAIL_FIELD_MAX_LENGTH,
        *,
        lowercase: bool = False,
        null: Literal[True],
        **kwargs: Any,
    ) -> None: ...

    def __init__(self, max_length: int = EMAIL_FIELD_MAX_LENGTH, *, lowercase: bool = False, **kwargs: Any) -> None:
        if not isinstance(lowercase, bool):
            raise ConfigurationError(f"EmailField: lowercase must be a bool, got {lowercase!r}")
        self.lowercase = lowercase
        # CharField's overloads bind self to CharField[str] or CharField[str | None] - TStr is one of them.
        super().__init__(max_length, **kwargs)  # type: ignore[misc]
        #: The validator checking the address - made by the write codec itself.
        self.format_validator = EmailValidator()
        self.validators.append(self.format_validator)

    @property
    def constraints(self) -> dict[str, Any]:
        return {**super().constraints, "format": "email"}

    def get_normalized_address(self, address: str) -> str:
        """The address as it is written.

        Args:
            address: The address.

        Returns:
            The address with its domain lowercase - all of it with ``lowercase``.
        """
        if self.lowercase:
            return address.lower()
        local_part, separator, domain = address.rpartition("@")
        if not separator:
            return address
        return f"{local_part}@{domain.lower()}"

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        if isinstance(value, str):
            value = self.get_normalized_address(value)
        return super().to_db_value(value, instance)
