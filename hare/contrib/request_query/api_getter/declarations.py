"""What ``ApiGetter`` reports through ``Observers`` - ``Observers.observe(ApiRequestExecuted, ...)`` - and
the value it settles for each target."""

from __future__ import annotations

import dataclasses
from typing import Any, ClassVar


@dataclasses.dataclass(frozen=True, slots=True)
class ApiRequestExecuted:
    """A request to a source - each attempt of it. The request's headers aren't part of it.

    Attributes:
        source_name: The source.
        url: The URL asked, without its query, its fragment and the credentials in it.
        status_code: The answer's status, None when the connection failed.
        duration_seconds: How long the request took.
        attempt: How many repeats came before it - 0 for the first request.
        error: What failed the connection, None with an answer.
    """

    observed_by_model: ClassVar[bool] = False

    source_name: str
    url: str
    status_code: int | None
    duration_seconds: float
    attempt: int
    error: str | None


@dataclasses.dataclass(slots=True)
class ApiTargetValue:
    """The value a target of an object gets, as the mappings setting it settle it.

    Attributes:
        container: The object the target is an attribute or a key of.
        set_name: The target's name in it.
        set_key: The target's whole path, for the errors.
        value: The value so far.
        has_value: Whether the value so far isn't empty.
        required: Whether a mapping requires a value.
        default: The value of the target left empty.
    """

    container: Any
    set_name: str
    set_key: str
    value: Any = None
    has_value: bool = False
    required: bool = False
    default: Any = None
