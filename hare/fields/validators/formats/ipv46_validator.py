from __future__ import annotations

from typing import Any

from hare.exceptions import ValidationError
from hare.fields.validators.formats.ipv4_validator import validate_ipv4_address
from hare.fields.validators.formats.ipv6_validator import validate_ipv6_address
from hare.fields.validators.validator import Validator


class IPv46Validator(Validator):
    """
    A validator to validate whether the given value is valid IPv4Address or IPv6Address or not.
    """

    def __call__(self, value: Any) -> None:
        try:
            validate_ipv4_address(value)
        except ValidationError:
            try:
                validate_ipv6_address(value)
            except ValidationError:
                self._raise(f"'{value}' is not a valid IPv4 or IPv6 address.")


validate_ipv46_address = IPv46Validator()


validate_ipv46_address.__doc__ = "Pre-configured IPv46Validator instance."
