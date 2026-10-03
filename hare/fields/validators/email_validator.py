import re
from functools import cached_property

from hare.exceptions import ValidationError
from hare.fields.constants import DOMAIN_REGEX, HOSTNAME_REGEX, TLD_NO_FQDN_REGEX
from hare.fields.validators.exceptions import InvalidEmailAddress
from hare.fields.validators.ipv46_validator import validate_ipv46_address
from hare.fields.validators.validator import Validator


class EmailValidator(Validator):
    """Validates an email address by RFC 3696.

    Args:
        allowed_domains: The only domains accepted, when given.
        message: Replaces the default error message.

    Raises:
        InvalidEmailAddress: The value isn't a valid email address.
    """

    # The maximum length of an email is 320 characters per RFC 3696 section 3
    MAX_EMAIL_LENGTH = 320

    USER_REGEX = (
        # dot-atom
        r"(^[-!#$%&'*+/=?^_`{}|~0-9A-Z]+(\.[-!#$%&'*+/=?^_`{}|~0-9A-Z]+)*\Z"
        # quoted-string
        r'|^"([\001-\010\013\014\016-\037!#-\[\]-\177]|\\[\001-\011\013\014\016-\177])'
        r'*"\Z)'
    )
    LITERAL_REGEX = (
        # literal form, ipv4 or ipv6 address (SMTP 4.1.3)
        r"\[([A-F0-9:.]+)\]\Z"
    )

    @cached_property
    def _user_regex(self) -> re.Pattern[str]:
        return re.compile(self.USER_REGEX, re.IGNORECASE)

    @cached_property
    def _domain_regex(self) -> re.Pattern[str]:
        return re.compile(r"^" + HOSTNAME_REGEX + DOMAIN_REGEX + TLD_NO_FQDN_REGEX + r"\Z", re.IGNORECASE)

    @cached_property
    def _literal_regex(self) -> re.Pattern[str]:
        return re.compile(self.LITERAL_REGEX, re.IGNORECASE)

    def _validate_domain_part(self, domain_part: str) -> bool:
        if self._domain_regex.match(domain_part):
            return True

        if literal_match := self._literal_regex.match(domain_part):
            ip_address = literal_match[1]
            try:
                validate_ipv46_address(ip_address)
                return True
            except ValidationError:
                pass
        return False

    def __init__(self, allowed_domains: list[str] | None = None, message: str | None = None) -> None:
        self.allowed_domains: list[str] = allowed_domains or []
        super().__init__(message)

    def __call__(self, value: str) -> None:
        if value is None:
            raise InvalidEmailAddress(self.message)
        if "@" not in value or len(value) > self.MAX_EMAIL_LENGTH:
            raise InvalidEmailAddress(self.message)

        user_part, domain_part = value.rsplit("@", 1)

        if not self._user_regex.match(user_part):
            raise InvalidEmailAddress(self.message)

        if domain_part not in self.allowed_domains and not self._validate_domain_part(domain_part):
            raise InvalidEmailAddress(self.message)


validate_email = EmailValidator()


validate_email.__doc__ = "Pre-configured EmailValidator instance."
