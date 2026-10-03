import re
from functools import cached_property

from hare.fields.constants import DOMAIN_REGEX, HOSTNAME_REGEX, TLD_REGEX
from hare.fields.validators.exceptions import InvalidDomainName
from hare.fields.validators.validator import Validator


class DomainNameValidator(Validator):
    """Validates a domain name by RFC 1034 and RFC 1123, internationalized ones too when
    ``accept_idna``.

    Args:
        accept_idna: Accept internationalized domain names - True by default.
        message: Replaces the default error message.

    Raises:
        InvalidDomainName: The value isn't a valid domain name.
    """

    ASCII_ONLY_HOSTNAME_REGEX = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    ASCII_ONLY_DOMAIN_REGEX = r"(?:\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*"
    ASCII_ONLY_TLD_REGEX = (
        r"\."  # dot
        r"(?!-)"  # can't start with a dash
        r"(?:[a-zA-Z0-9-]{2,63})"  # domain label
        r"(?<!-)"  # can't end with a dash
        r"\.?"  # may have a trailing dot
    )
    MAX_DOMAIN_LENGTH = 255

    @cached_property
    def _accept_idna_regex(self) -> re.Pattern[str]:
        # \Z, not $ - $ matches immediately before a trailing "\n" too, letting a value like
        # "example.com\n" pass as if it were "example.com".
        return re.compile(r"^" + HOSTNAME_REGEX + DOMAIN_REGEX + TLD_REGEX + r"\Z", re.IGNORECASE)

    @cached_property
    def _do_not_accept_idna_regex(self) -> re.Pattern[str]:
        return re.compile(
            r"^" + self.ASCII_ONLY_HOSTNAME_REGEX + self.ASCII_ONLY_DOMAIN_REGEX + self.ASCII_ONLY_TLD_REGEX + r"\Z",
            re.IGNORECASE,
        )

    def __init__(self, accept_idna: bool = True, message: str | None = None) -> None:
        self.accept_idna = accept_idna
        super().__init__(message)

    @property
    def regex(self) -> re.Pattern[str]:
        """The pattern a domain name must match - compiled on first use, not when the validator is
        created: this module creates one (``validate_domain_name``) on import, and the IDN pattern
        is large enough to dominate the import of ``hare.fields``."""
        return self._accept_idna_regex if self.accept_idna else self._do_not_accept_idna_regex

    def __call__(self, value: str) -> None:
        if value is None:
            raise InvalidDomainName(self.message)
        if len(value) > self.MAX_DOMAIN_LENGTH:
            raise InvalidDomainName(self.message)

        if not (self.accept_idna or value.isascii()):
            raise InvalidDomainName(self.message)

        if not self.regex.search(value):
            raise InvalidDomainName(self.message)


validate_domain_name = DomainNameValidator()


validate_domain_name.__doc__ = "Pre-configured DomainNameValidator instance."
