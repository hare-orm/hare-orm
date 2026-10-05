from __future__ import annotations

import ipaddress
from typing import Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.fields.validators.validator import Validator


class IPv4Validator(Validator):
    """Checks a value is an IPv4 address."""

    def __call__(self, value: Any) -> None:
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            self._raise(f"'{ValueDescriptions.get_shown_text(value)}' is not a valid IPv4 address.")


validate_ipv4_address = IPv4Validator()


validate_ipv4_address.__doc__ = "Pre-configured IPv4Validator instance."
