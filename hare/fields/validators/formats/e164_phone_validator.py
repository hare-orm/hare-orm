from __future__ import annotations

import re
from typing import Any

from hare.fields.constants import E164_PHONE_PATTERN
from hare.fields.validators.exceptions import InvalidPhoneNumber
from hare.fields.validators.validator import Validator


class E164PhoneValidator(Validator):
    """Validates a phone number in E.164 form: ``+``, a country code not starting with 0 and at most
    15 digits in all, with nothing between them (``+16502530000``).

    Args:
        message: Replaces the default error message.

    Raises:
        InvalidPhoneNumber: The value isn't an E.164 number.
    """

    regex = re.compile(E164_PHONE_PATTERN)

    def __call__(self, value: Any) -> None:
        if not isinstance(value, str) or self.regex.fullmatch(value) is None:
            raise InvalidPhoneNumber(self.message)


validate_e164_phone = E164PhoneValidator()


validate_e164_phone.__doc__ = "Pre-configured E164PhoneValidator instance."
