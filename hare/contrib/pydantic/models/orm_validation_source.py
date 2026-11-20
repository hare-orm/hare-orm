from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.models import Model


@dataclass(slots=True)
class OrmValidationSource:
    """A validation's temporary source, cleared when its handler returns or raises.

    A task created by a validator inherits this entry by reference. Clearing ``obj``
    prevents that task from retaining or borrowing the completed validation's ORM input.
    Completed schemas keep their explicitly attached source independently.
    """

    schema: type[Any]
    obj: Model | None
