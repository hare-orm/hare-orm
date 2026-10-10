from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal, overload

from hare.exceptions import ConfigurationError
from hare.fields.data.constants import URL_FIELD_MAX_LENGTH, URL_FIELD_SCHEMES, URL_SCHEME_PATTERN
from hare.fields.data.text.char_field import CharField, TStr
from hare.fields.validators.formats.url_validator import URLValidator


class URLField(CharField[TStr]):
    """A URL with a host, checked by ``URLValidator`` - written as it is given.

    Args:
        max_length: The most characters the URL may have - 2048 by default.
        schemes: The schemes accepted, lowercase - ``("http", "https")`` by default. A URL's
            scheme is compared lowercase: ``HTTPS://example.com`` is accepted.
    """

    @overload
    def __init__(
        self: URLField[str],
        max_length: int = URL_FIELD_MAX_LENGTH,
        *,
        schemes: Sequence[str] = URL_FIELD_SCHEMES,
        null: Literal[False] = False,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self: URLField[str | None],
        max_length: int = URL_FIELD_MAX_LENGTH,
        *,
        schemes: Sequence[str] = URL_FIELD_SCHEMES,
        null: Literal[True],
        **kwargs: Any,
    ) -> None: ...

    def __init__(
        self, max_length: int = URL_FIELD_MAX_LENGTH, *, schemes: Sequence[str] = URL_FIELD_SCHEMES, **kwargs: Any
    ) -> None:
        if isinstance(schemes, str) or not isinstance(schemes, Sequence) or not schemes:
            raise ConfigurationError(f"URLField: schemes must be a non-empty list of schemes, got {schemes!r}")
        for scheme in schemes:
            if not isinstance(scheme, str) or re.fullmatch(URL_SCHEME_PATTERN, scheme) is None:
                raise ConfigurationError(
                    f"URLField: {scheme!r} isn't a scheme - a lowercase letter, then lowercase letters, "
                    "digits, '+', '-' or '.'"
                )
        self.schemes = tuple(schemes)
        # CharField's overloads bind self to CharField[str] or CharField[str | None] - TStr is one of them.
        super().__init__(max_length, **kwargs)  # type: ignore[misc]
        #: The validator checking the URL - made by the write codec itself.
        self.format_validator = URLValidator(allowed_schemes=list(self.schemes))
        self.validators.append(self.format_validator)

    @property
    def constraints(self) -> dict[str, Any]:
        return {**super().constraints, "format": "uri"}
