from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, eq=False, slots=True)
class StructureIdentity:
    """An opaque object retained by a structural key and compared only by identity.

    SQL terms overload equality and hashing to build expressions. This wrapper never
    invokes either operation on a term, a field declaration or an extension object.
    Retaining the object also prevents reuse of its identity while a key is alive.
    """

    value: Any

    def __hash__(self) -> int:
        return id(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StructureIdentity) and self.value is other.value
