from __future__ import annotations

from typing import Any

from hare.exceptions import ValidationError


class InvalidURL(ValidationError):
    """A value that isn't a URL."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid URL")


class InvalidScheme(InvalidURL):
    """A URL of a scheme the validator doesn't allow."""

    def __init__(self, scheme: str, message: str | None = None) -> None:
        self.scheme = scheme
        self.message = message
        super().__init__(message or f"Invalid scheme: {scheme} is not allowed")

    def __reduce__(self) -> tuple[type[InvalidScheme], tuple[Any, ...]]:
        return self.__class__, (self.scheme, self.message)


class InvalidDomainName(ValidationError):
    """A value that isn't a domain name."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid domain name")


class InvalidEmailAddress(ValidationError):
    """A value that isn't an email address."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid email address")


class InvalidSlug(ValidationError):
    """A value that isn't a slug - letters, digits, hyphens and underscores."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid slug: only letters, digits, hyphens and underscores are allowed")


class InvalidPhoneNumber(ValidationError):
    """A value that isn't a phone number in the E.164 form."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid phone number")
