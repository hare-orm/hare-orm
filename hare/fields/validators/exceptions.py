from hare.exceptions import ValidationError


class InvalidURL(ValidationError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid URL")


class InvalidScheme(InvalidURL):
    def __init__(self, scheme: str, message: str | None = None) -> None:
        super().__init__(message or f"Invalid scheme: {scheme} is not allowed")


class InvalidDomainName(ValidationError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid domain name")


class InvalidEmailAddress(ValidationError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Invalid email address")
