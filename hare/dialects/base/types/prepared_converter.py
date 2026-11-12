from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class PreparedConverter:
    """A field-dependent conversion with the ordinary TypeMapping callable contract.

    Preparation binds declaration metadata and child callbacks, never input values
    or a model instance. A direct call prepares and invokes that same conversion;
    column writers and readers retain it for their operation or statement layout.
    This object stores a factory, not a cache of prepared fields.
    """

    factory: Callable[[Field[Any]], Callable[..., Any]]

    def prepare(self, field: Field[Any]) -> Callable[..., Any]:
        """Bind one declaration for reuse without retaining an input value or instance."""
        return self.factory(field)

    def __call__(self, field: Field[Any], *arguments: Any) -> Any:
        """Apply the prepared implementation through TypeMapping's direct-call contract."""
        return self.prepare(field)(*arguments)
