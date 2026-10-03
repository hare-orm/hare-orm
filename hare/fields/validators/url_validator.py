import re
from functools import cached_property
from urllib.parse import urlsplit

from hare.exceptions import ValidationError
from hare.fields.constants import DOMAIN_REGEX, HOSTNAME_REGEX, TLD_REGEX
from hare.fields.validators.exceptions import InvalidScheme, InvalidURL
from hare.fields.validators.ipv6_validator import validate_ipv6_address
from hare.fields.validators.validator import Validator


class URLValidator(Validator):
    """
    Validator for URLs.

    Validates URLs according to RFC 3986. Checks scheme, host, port, and path
    components. Supports HTTP, HTTPS, FTP, and FTPS schemes by default.

    Args:
        allowed_schemes: List of allowed URL schemes. Defaults to ["http", "https", "ftp", "ftps"].
        message: Overrides InvalidURL's and InvalidScheme's default error messages.

    Raises:
        InvalidURL: if the value is not a valid URL.
        InvalidScheme: if the URL scheme is not in the allowed list.
    """

    IPV4_REGEX = (
        r"(?:0|25[0-5]|2[0-4][0-9]|1[0-9]?[0-9]?|[1-9][0-9]?)"
        r"(?:\.(?:0|25[0-5]|2[0-4][0-9]|1[0-9]?[0-9]?|[1-9][0-9]?)){3}"
    )
    SIMPLE_IPV6_REGEX = r"\[[0-9a-f:.]+\]"  # (simple regex, validated later)
    ADVANCED_IPV6_REGEX = r"^\[(.+)\](?::[0-9]{1,5})?$"
    HOST_REGEX = "(" + HOSTNAME_REGEX + DOMAIN_REGEX + TLD_REGEX + "|localhost)"
    URL_REGEX = (
        r"^(?:[a-z0-9.+-]*)://"  # scheme is validated separately
        r"(?:[^\s:@/]+(?::[^\s:@/]*)?@)?"  # user:pass authentication
        r"(?:" + IPV4_REGEX + "|" + SIMPLE_IPV6_REGEX + "|" + HOST_REGEX + ")"
        r"(?::[0-9]{1,5})?"  # port
        r"(?:[/?#][^\s]*)?"  # resource path
        r"\Z"
    )
    UNSAFE_CHARS = frozenset("\t\r\n")
    MAX_URL_LENGTH = 2048

    # The longest host name, by RFC 1034 section 3.1.
    MAX_HOSTNAME_LENGTH = 253

    @cached_property
    def _url_regex(self) -> re.Pattern[str]:
        return re.compile(self.URL_REGEX, re.IGNORECASE)

    def __init__(self, allowed_schemes: list[str] | None = None, message: str | None = None) -> None:
        self.allowed_schemes = allowed_schemes or ["http", "https", "ftp", "ftps"]
        super().__init__(message)

    def __call__(self, value: str) -> None:
        if value is None:
            raise InvalidURL(self.message)
        if len(value) > self.MAX_URL_LENGTH:
            raise InvalidURL(self.message)

        if self.UNSAFE_CHARS.intersection(value):
            raise InvalidURL(self.message)

        try:
            split_url = urlsplit(value)
        except ValueError:
            raise InvalidURL(self.message)

        if split_url.scheme.lower() not in self.allowed_schemes:
            raise InvalidScheme(split_url.scheme.lower(), self.message)

        if split_url.hostname is None or len(split_url.hostname) > self.MAX_HOSTNAME_LENGTH:
            raise InvalidURL(self.message)

        if not self._url_regex.search(value):
            raise InvalidURL(self.message)

        # Now verify IPv6 in the netloc part
        host_match = re.search(self.ADVANCED_IPV6_REGEX, split_url.netloc)
        if host_match:
            potential_ip = host_match[1]
            try:
                validate_ipv6_address(potential_ip)
            except ValidationError:
                raise InvalidURL(self.message)


validate_url = URLValidator()


validate_url.__doc__ = "Pre-configured URLValidator instance."
