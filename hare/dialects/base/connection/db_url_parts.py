from __future__ import annotations

import re
import urllib.parse as urlparse
from collections.abc import Callable
from dataclasses import dataclass, field

from hare.dialects.base.connection.constants import DB_URL_INVALID_ESCAPE_PATTERN, DB_URL_USERINFO_DELIMITERS
from hare.dialects.base.connection.messages import (
    DB_URL_FRAGMENT,
    ENCODE_DB_URL_USERINFO,
    INVALID_DB_URL_PORT,
    MALFORMED_DB_URL,
)
from hare.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class DbUrlParts:
    """A connection URL parsed once, with decoded authority and query components.

    The driver's path policy consumes the original parsed URL: file URLs have no userinfo and
    retain their relative, absolute and Windows path conventions. Component decoding never
    treats a decoded delimiter as URL syntax. Raw credentials are excluded from repr.
    """

    parsed: urlparse.ParseResult = field(repr=False)
    has_userinfo: bool
    host: str | None
    port: int | None
    username: str | None = field(repr=False)
    password: str | None = field(repr=False)
    query: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def parse(cls, db_url: str, *, has_userinfo: bool) -> DbUrlParts:
        """Parse the URL grammar without guessing or repairing credentials.

        Invalid components raise an error which never includes text from the URL. Network
        URLs permit one userinfo separator; reserved delimiters in either credential must
        be percent-encoded. Blank query values remain explicit values.
        """
        try:
            parsed = urlparse.urlparse(db_url)
        except ValueError:
            raise ConfigurationError(MALFORMED_DB_URL) from None
        if parsed.fragment:
            raise ConfigurationError(DB_URL_FRAGMENT)
        host = username = password = None
        port = None
        if has_userinfo:
            if parsed.netloc.count("@") > 1:
                raise ConfigurationError(ENCODE_DB_URL_USERINFO)
            for component in (parsed.username, parsed.password):
                if component is not None and (
                    any(character in DB_URL_USERINFO_DELIMITERS for character in component)
                    or re.search(DB_URL_INVALID_ESCAPE_PATTERN, component)
                ):
                    raise ConfigurationError(ENCODE_DB_URL_USERINFO)
            username = urlparse.unquote(parsed.username) if parsed.username else None
            password = urlparse.unquote(parsed.password) if parsed.password is not None else None
            try:
                port = parsed.port
            except ValueError:
                raise ConfigurationError(INVALID_DB_URL_PORT) from None
            host = urlparse.unquote(parsed.hostname) if parsed.hostname else None
            if host is not None and host.startswith("/"):
                # hostname normalizes DNS case; a percent-encoded socket path is case-sensitive.
                host = urlparse.unquote(parsed.netloc.rsplit("@", 1)[-1].partition(":")[0])
        return cls(
            parsed,
            has_userinfo,
            host,
            port,
            username,
            password,
            tuple(urlparse.parse_qsl(parsed.query, keep_blank_values=True)),
        )

    def masked(self, is_secret_key: Callable[[str], bool], mask: str) -> str:
        """Render the same components with userinfo passwords and secret query values hidden."""
        authority = self.parsed.netloc
        if self.has_userinfo and "@" in authority:
            userinfo, separator, host = authority.partition("@")
            username, password_separator, password = userinfo.partition(":")
            if password_separator:
                userinfo = f"{username}:{mask if password else ''}"
            authority = f"{userinfo}{separator}{host}"
        parameters = []
        for parameter in self.parsed.query.split("&"):
            name, equals, value = parameter.partition("=")
            if equals and is_secret_key(urlparse.unquote_plus(name)):
                value = mask
            parameters.append(f"{name}{equals}{value}")
        path = self.parsed.path + (f";{self.parsed.params}" if self.parsed.params else "")
        query = "&".join(parameters)
        suffix = f"?{query}" if query else ""
        return f"{self.parsed.scheme}://{authority}{path}{suffix}"
