from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.contrib.factories.factory_reference import FactoryReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.factories.model_factory import ModelFactory
    from hare.models import Model


class SubFactory:
    """A related object another factory makes, created with the object - a built object gets none: a
    model takes only a saved related object, given to ``build()``::

        team = SubFactory(TeamFactory, name="core")
        target = SubFactory("myapp.factories.PostFactory")  # a GenericForeignKey's target too

    Args:
        factory: The factory, or its dotted path.
        values: Values the related object gets, by field name.
    """

    def __init__(self, factory: type[ModelFactory[Any]] | str, **values: Any) -> None:
        self.reference = FactoryReference(factory)
        self.values = values

    async def create(self) -> Model:
        """The related object, saved."""
        return await self.reference.get_factory().create(**self.values)
