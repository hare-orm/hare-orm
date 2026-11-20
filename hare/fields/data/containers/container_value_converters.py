from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from hare.fields.field import Field


class ContainerValueConverters(Protocol):
    """Scalar conversions used by a container's shared traversal, independent of its wire format.

    Database type registries and cursor codecs supply these operations; the container
    retains its shape, nullable children, custom hooks and indexed error paths.
    """

    container_values_always_nullable: bool

    def get_db_writer(self, field: Field[Any]) -> Callable[[Any, Any], Any]: ...

    def get_python_reader(self, field: Field[Any]) -> Callable[[Any], Any]: ...
