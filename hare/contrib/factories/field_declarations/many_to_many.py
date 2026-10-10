from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.contrib.factories.factory_reference import FactoryReference
from hare.contrib.factories.field_declarations.post_declaration import PostDeclaration

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.factories.model_factory import ModelFactory
    from hare.models import Model


class ManyToMany(PostDeclaration):
    """The many-to-many links of the created object - to objects another factory makes::

        tags = ManyToMany(TagFactory, size=2)

    Given to ``create()`` under its name, objects are linked instead: ``create(tags=[python, orm])``.

    Args:
        factory: The factory of the linked objects, or its dotted path - None links only what
            ``create()`` is given.
        size: How many objects the factory makes.
        values: Values the made objects get, by field name.
    """

    def __init__(self, factory: type[ModelFactory[Any]] | str | None = None, *, size: int = 0, **values: Any) -> None:
        self.reference = FactoryReference(factory) if factory is not None else None
        self.size = size
        self.values = values

    async def apply(self, obj: Model, name: str, given: Any) -> None:
        if given is not None:
            linked = list(given)
        elif self.reference is not None and self.size:
            linked = await self.reference.get_factory().create_batch(self.size, **self.values)
        else:
            return
        if linked:
            await getattr(obj, name).add(*linked)
