from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

import pydantic
from pydantic import BaseModel, ConfigDict

from hare.contrib.pydantic.descriptions.pydantic_serialization_description import PydanticSerializationDescription
from hare.contrib.pydantic.messages import RECURSIVE_SCHEMA_FETCH
from hare.exceptions import ConfigurationError, DoesNotExist, QueryError
from hare.query.queryset import QuerySet
from hare.query.relation_loading.prefetching.prefetch_related_objects import prefetch_related_objects

if TYPE_CHECKING:
    from typing import Self

    from hare.models import Model
    from hare.query.queryset import QuerySetSingle


class PydanticModel(BaseModel):
    """Pydantic BaseModel for Hare objects.

    Provides extra methods on top of Pydantic's own model properties
    (https://docs.pydantic.dev/latest/usage/models/#model-properties).
    """

    model_config = ConfigDict(from_attributes=True)

    #: The model the schema was made of - set by ``pydantic_model_creator()``; None for a schema written by
    #: hand.
    source_model: ClassVar[type[Any] | None] = None
    #: The paths of the related values the schema shows (``"author.name"``) - their relations are fetched.
    related_value_paths: ClassVar[tuple[str, ...]] = ()

    @pydantic.model_validator(mode="wrap")
    @classmethod
    def _hare_wrap(cls, values: Any, handler: Callable[[Any], Any]) -> Any:
        orm_obj = values if hasattr(values, "_meta") else None
        if orm_obj is not None:
            cls._raise_if_relation_not_fetched(orm_obj)
        instance = handler(values)
        if orm_obj is not None:
            object.__setattr__(instance, "__orm_obj__", orm_obj)
        return instance

    @classmethod
    def _raise_if_relation_not_fetched(cls, obj: Model) -> None:
        """Raises for a field the schema reads that isn't loaded on ``obj``: an unfetched relation
        would fail validation with an unrelated error, and an unloaded plain field would silently
        take the schema's default.

        Args:
            obj: The model instance about to be validated.

        Raises:
            NoValuesFetched: A field the schema reads isn't loaded.
        """
        description = PydanticSerializationDescription.get(cls)
        for source in description.field_sources:
            source.check_loaded(obj)

    @classmethod
    def _get_fetch_fields(cls) -> tuple[str, ...]:
        """The finite fetch plan compiled alongside this schema's field checks."""
        paths = PydanticSerializationDescription.get(cls).fetch_fields
        if paths is None:
            raise ConfigurationError(RECURSIVE_SCHEMA_FETCH)
        return paths

    @classmethod
    async def from_hare_orm(cls, obj: Model) -> Self:
        """Builds the pydantic model from ``obj``, fetching its relations first. ``model_validate()`` is
        the synchronous variant - fetch or exclude the relations yourself.

        Args:
            obj: The model instance.
        """
        fetch_fields = cls._get_fetch_fields()
        await prefetch_related_objects([obj], *fetch_fields)
        return cls.model_validate(obj)

    @classmethod
    async def from_queryset_single(cls, queryset: QuerySetSingle[Model]) -> Self:
        """Returns a serializable pydantic model instance for a single model from the provided
        queryset.

        This will prefetch all the relations automatically.

        Args:
            queryset: a queryset on the model this PydanticModel is based on.

        Raises:
            DoesNotExist: The query reads no row (``first()`` of no rows).
            QueryError: The query reads a list of rows, not one - ``from_queryset()`` takes it.
        """
        fetch_fields = cls._get_fetch_fields()
        # A queryset passed where one row is expected reads a list.
        obj: Any = await queryset.prefetch_related(*fetch_fields)
        if isinstance(obj, list):
            raise QueryError(
                "from_queryset_single() takes a query of one row - .get(...) or .first() - not a queryset "
                "of many: from_queryset() takes that"
            )
        if obj is None:
            raise DoesNotExist("from_queryset_single(): the query reads no row")
        return cls.model_validate(obj)

    @classmethod
    async def from_queryset(cls, queryset: QuerySet[Model]) -> list[Self]:
        """Returns a serializable pydantic model instance that contains a list of models, from
        the provided queryset.

        This will prefetch all the relations automatically.

        Args:
            queryset: a queryset on the model this PydanticModel is based on.
        """
        fetch_fields = cls._get_fetch_fields()
        return [cls.model_validate(obj) for obj in await queryset.prefetch_related(*fetch_fields)]
