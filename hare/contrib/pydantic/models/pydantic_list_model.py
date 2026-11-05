from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import RootModel

from hare.query.queryset import QuerySet

if TYPE_CHECKING:
    from typing import Self

    from hare.models import Model


class PydanticListModel(RootModel[Any]):
    """Pydantic BaseModel for a list of Hare models.

    Provides extra methods on top of Pydantic's own model properties
    (https://docs.pydantic.dev/latest/concepts/models/#model-methods-and-properties).
    """

    @classmethod
    async def from_queryset(cls, queryset: QuerySet[Model]) -> Self:
        """Returns a serializable pydantic model instance that contains a list of models, from
        the provided queryset.

        Delegates relation loading and item validation to the configured submodel's
        ``from_queryset()``, then validates the list wrapper.

        Args:
            queryset: a queryset on the model this PydanticListModel is based on.
        """
        submodel = cls.model_config["submodel"]  # type: ignore[typeddict-item]
        return cls.model_validate(await submodel.from_queryset(queryset))
