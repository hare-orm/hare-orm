from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.contrib.factories.factory_reference import FactoryReference
from hare.contrib.factories.field_declarations.post_declaration import PostDeclaration

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.factories.model_factory import ModelFactory
    from hare.models import Model


class RelatedFactory(PostDeclaration):
    """Rows pointing at the created object, made by another factory::

        posts = RelatedFactory(PostFactory, "author", size=2)

    Given to ``create()`` under its name, a number replaces ``size``.

    Args:
        factory: The factory of the rows, or its dotted path.
        related_name: The rows' field pointing at the object.
        size: How many rows.
        values: Values the rows get, by field name.
    """

    def __init__(
        self, factory: type[ModelFactory[Any]] | str, related_name: str, *, size: int = 1, **values: Any
    ) -> None:
        self.reference = FactoryReference(factory)
        self.related_name = related_name
        self.size = size
        self.values = values

    async def apply(self, obj: Model, name: str, given: Any) -> None:
        size = self.size if given is None else given
        await self.reference.get_factory().create_batch(size, **{self.related_name: obj, **self.values})
