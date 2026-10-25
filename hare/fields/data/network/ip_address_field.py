from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.fields.data.constants import IP_ADDRESS_VALUE_TYPE
from hare.fields.data.network.constants import IPV6_ADDRESS_TEXT_LENGTH
from hare.fields.field import Field
from hare.sql.functions.address_text import AddressText

if TYPE_CHECKING:
    from hare.models import Model
    from hare.sql.terms.term import Term


class IPAddressField(Field[Any]):
    """An IPv4 or IPv6 host address - an ``IPv4Address`` or ``IPv6Address``; its text is taken too.
    A database with an address type stores it as one - an IPv4 address mapped into an IPv6 one where the
    type holds IPv6 alone, read back as IPv4 - any other as its text.
    """

    argument_value_type = IP_ADDRESS_VALUE_TYPE

    field_type = str
    keeps_native_db_values = False

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"VARCHAR({IPV6_ADDRESS_TEXT_LENGTH})"

    def get_python_type(self) -> Any:
        return ipaddress.IPv4Address | ipaddress.IPv6Address

    def get_accepted_value_types(self) -> tuple[Any, ...]:
        return (ipaddress.IPv4Address, ipaddress.IPv6Address)

    def parse(self, value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        """The address of a value.

        Args:
            value: An address or its text.

        Returns:
            The address - an IPv4 address mapped into IPv6 as the IPv4 one.

        Raises:
            ValueError: The value is no address of the field's family.
        """
        address = value if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address) else None
        if address is None:
            address = ipaddress.ip_address(value.decode() if isinstance(value, bytes) else str(value))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped
        return address

    def get_address(self, value: Any) -> Any:
        """The ``ipaddress`` address of a value - an IPv4-mapped IPv6 address as its IPv4 one.

        Args:
            value: The value - text or an ``ipaddress`` object.

        Returns:
            The object.

        Raises:
            ValidationError: The value isn't one of the field's type.
        """
        validation_error = None
        try:
            return self.parse(value)
        except (ValueError, TypeError) as error:
            validation_error = self.get_validation_error(error, value)
        raise validation_error

    def get_like_text_function(self) -> Callable[[Term], Term] | None:
        """The address's text - an IPv4 address as its IPv4 text on every database."""
        return AddressText

    def to_python(self, value: Any) -> Any:
        return None if value is None else self.get_address(value)

    def from_db_value(self, value: Any) -> Any:
        return self.to_python(value)

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        if value is None:
            self.validate(value)
            return None
        address = self.get_address(value)
        self.validate(str(address))
        return str(address)

    def to_db_zoneless_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        """The address's text for a column of an address type - which holds no zone of an IPv6 address.

        Args:
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The text, None for None.

        Raises:
            ValidationError: The address names a zone (``fe80::1%eth0``) - the column would lose it.
        """
        text = self.to_db_value(value, instance)
        # An address naming a zone is written with it after a "%" - no other address text holds one.
        if text is not None and "%" in text:
            raise ValidationError(
                f"{self.model_field_name}: {text} names a zone of the address, which an address column of the "
                "database holds no place for"
            )
        return text
