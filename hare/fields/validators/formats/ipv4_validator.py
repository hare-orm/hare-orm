from __future__ import annotations

import ipaddress
from typing import Any

from hare.fields.validators.validator import Validator


class IPv4Validator(Validator):
    """
    A validator to validate whether the given value is valid IPv4Address or not.
    """

    def __call__(self, value: Any) -> None:
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            self._raise(f"'{value}' is not a valid IPv4 address.")


validate_ipv4_address = IPv4Validator()


validate_ipv4_address.__doc__ = "Pre-configured IPv4Validator instance."
