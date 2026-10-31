from __future__ import annotations

from uuid import UUID

from hare.exceptions import ValidationError
from hare.fields.constants import UUID_RFC_VARIANT_BITS, UUID_VERSION_AND_VARIANT_MASK
from hare.fields.validators.validator import Validator


class UUIDVersionValidator(Validator):
    """Checks a UUID is of one version of RFC 9562 - ``UUIDField(version=7)``.

    Args:
        version: The version.
        message: Replaces the default error message when given.
    """

    def __init__(self, version: int, message: str | None = None) -> None:
        self.version = version
        #: The version and the variant bits of a UUID of the version - compared on the UUID's integer, cheaper
        #: than UUID.version, which works out the variant first.
        self.version_and_variant_bits = version << 76 | UUID_RFC_VARIANT_BITS
        super().__init__(message)

    def __call__(self, value: UUID | None) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        if value.int & UUID_VERSION_AND_VARIANT_MASK != self.version_and_variant_bits:
            self._raise(f"expected a version {self.version} UUID, got {value} of version {value.version}")
