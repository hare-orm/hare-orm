from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.constants import (
    MAC_ADDRESS_DIGITS_PATTERN,
    MAC_ADDRESS_SEPARATOR_PATTERN,
    POSTGRESQL_MACADDR_TYPE,
)
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup


class MacAddressField(Field[str]):
    """A 6-byte MAC address - a ``macaddr`` column. A value is text in any of the usual forms
    (``08:00:2B:01:02:03``, ``08-00-2b-01-02-03``, ``0800.2b01.0203``, ``08002b010203``) and is read
    and written as ``08:00:2b:01:02:03``; another text raises ``ValidationError``. It filters by
    equality, comparison, membership, ``isnull`` and ``range``.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})
    SQL_TYPE = POSTGRESQL_MACADDR_TYPE
    field_type = str

    def normalize(self, value: Any) -> str:
        """The address as lowercase, colon-separated octets.

        Raises:
            ValidationError: The value isn't text of a MAC address.
        """
        digits = MAC_ADDRESS_SEPARATOR_PATTERN.sub("", value) if isinstance(value, str) else ""
        if not MAC_ADDRESS_DIGITS_PATTERN.fullmatch(digits):
            raise self.get_validation_error(ValueError(f"{value!r} is not a MAC address"), value)
        digits = digits.lower()
        return ":".join(digits[index : index + 2] for index in range(0, len(digits), 2))

    def to_python(self, value: Any) -> Any:
        return None if value is None else self.normalize(str(value))

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        if value is None:
            self.validate(value)
            return None
        address = self.normalize(value)
        self.validate(address)
        return address

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the filters package imports the fields package.
        from hare.dialects.postgresql.lookups.network.postgresql_network_field_lookups import (
            PostgresqlNetworkFieldLookups,
        )
        from hare.query.filters.lookups.field_lookups import FieldLookups

        generic = FieldLookups.get_generic(self)
        return {name: generic[name] for name in PostgresqlNetworkFieldLookups.KEPT_GENERIC_LOOKUPS}
