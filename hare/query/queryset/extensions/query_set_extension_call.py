from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    pass


@dataclass(frozen=True, slots=True)
class QuerySetExtensionCall:
    """One call of a dialect's QuerySet method, kept on the queryset until it's built.

    Attributes:
        name: The method's name.
        args: The positional arguments.
        kwargs: The keyword arguments.
    """

    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
