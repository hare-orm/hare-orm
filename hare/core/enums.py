from enum import StrEnum


class ApplicationScope(StrEnum):
    """An explicit absence of application resources, distinct from global fallback lookup."""

    DETACHED = "detached"
