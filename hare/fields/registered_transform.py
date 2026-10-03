from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field


@dataclass(frozen=True, slots=True)
class RegisteredTransform:
    """A path segment registered on a field class.

    Attributes:
        get_transform: Builds the transform for one field: the function building the term the
            segment reads, and the field of the value it reads.
        required_extension: The database extension the transform needs, None for none.
    """

    get_transform: Callable[[Field[Any]], tuple[Callable[[Any], Any], Field[Any]]]
    required_extension: str | None = None
