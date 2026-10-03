from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import RootModel

from hare.query.queryset import QuerySet

if TYPE_CHECKING:  # pragma: nocoverage
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

        This will prefetch all the relations automatically.

        Args:
            queryset: a queryset on the model this PydanticListModel is based on.
        """
        submodel = cls.model_config["submodel"]  # type: ignore[typeddict-item]
        fetch_fields = submodel._get_fetch_fields()
        return cls.model_validate(submodel._validate_prefetched(list(await queryset.prefetch_related(*fetch_fields))))
