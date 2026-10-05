from __future__ import annotations

import ipaddress
from typing import Any

from hare.fields.validators.validator import Validator


class IPv6Validator(Validator):
    """
    A validator to validate whether the given value is valid IPv6Address or not.
    """

    def __call__(self, value: Any) -> None:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            self._raise(f"'{value}' is not a valid IPv6 address.")


validate_ipv6_address = IPv6Validator()


validate_ipv6_address.__doc__ = "Pre-configured IPv6Validator instance."
