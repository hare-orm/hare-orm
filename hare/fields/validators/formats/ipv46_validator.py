from __future__ import annotations

import ipaddress
from typing import Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.fields.validators.validator import Validator


class IPv46Validator(Validator):
    """Checks a value is an IPv4 or an IPv6 address."""

    def __call__(self, value: Any) -> None:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            self._raise(f"'{ValueDescriptions.get_shown_text(value)}' is not a valid IPv4 or IPv6 address.")


validate_ipv46_address = IPv46Validator()


validate_ipv46_address.__doc__ = "Pre-configured IPv46Validator instance."
