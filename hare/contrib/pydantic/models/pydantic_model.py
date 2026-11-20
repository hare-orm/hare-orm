from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar

import pydantic
from pydantic import BaseModel, ConfigDict

from hare.contrib.pydantic.descriptions.pydantic_serialization_description import PydanticSerializationDescription
from hare.contrib.pydantic.messages import RECURSIVE_SCHEMA_FETCH
from hare.contrib.pydantic.models.orm_validation_source import OrmValidationSource
from hare.exceptions import ConfigurationError, DoesNotExist, QueryError
from hare.models import Model
from hare.query.queryset import QuerySet
from hare.query.relation_loading.prefetching.prefetch_related_objects import prefetch_related_objects

if TYPE_CHECKING:
    from typing import Self

    from hare.models.model_binding import ModelBinding
    from hare.query.queryset import QuerySetSingle


class PydanticModel(BaseModel):
    """Pydantic BaseModel for Hare objects.

    Provides extra methods on top of Pydantic's own model properties
    (https://docs.pydantic.dev/latest/usage/models/#model-properties).
    """

    model_config = ConfigDict(from_attributes=True)

    #: The model of source_binding, derived when a generated or handwritten schema selects its source.
    source_model: ClassVar[type[Any] | None] = None
    #: The structural graph used to generate this schema; no application resources are retained.
    source_binding: ClassVar[ModelBinding | None] = None
    #: The paths of the related values the schema shows (``"author.name"``) - their relations are fetched.
    related_value_paths: ClassVar[tuple[str, ...]] = ()
    #: The source while validators run, before their handler returns the completed instance.
    #: The schema class prevents another schema's computed field from reading this source.
    _orm_validation_source: ClassVar[ContextVar[OrmValidationSource | None]] = ContextVar(
        "hare_pydantic_validation_source", default=None
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Capture a handwritten schema's explicit source through Pydantic's class hook."""
        super().__pydantic_init_subclass__(**kwargs)
        binding = cls.source_binding
        if binding is not None:
            if "source_model" in cls.__dict__ and cls.source_model is not binding._model:
                raise ConfigurationError("source_model must match the model of source_binding")
            cls._set_source_binding(binding)

    @classmethod
    def _set_source_binding(cls, binding: ModelBinding) -> None:
        """Publish one structural source and its derived model for generated and manual schemas."""
        cls.source_binding = binding
        cls.source_model = binding._model
        PydanticSerializationDescription.descriptions.forget_holder(cls)

    @pydantic.model_validator(mode="wrap")
    @classmethod
    def _hare_wrap(cls, values: Any, handler: Callable[[Any], Any]) -> Any:
        orm_obj = values if isinstance(values, Model) else None
        source = OrmValidationSource(cls, orm_obj) if orm_obj is not None else None
        token = PydanticModel._orm_validation_source.set(source)
        try:
            if orm_obj is not None:
                cls._raise_if_relation_not_fetched(orm_obj)
            instance = handler(values)
            if orm_obj is not None or not hasattr(instance, "__orm_obj__"):
                object.__setattr__(instance, "__orm_obj__", orm_obj)
            return instance
        finally:
            if source is not None:
                source.obj = None
            PydanticModel._orm_validation_source.reset(token)

    @staticmethod
    def _get_orm_source(instance: PydanticModel) -> Model | None:
        """Read the attached source, including while inherited validators build the instance.

        Nested validation of a plain payload explicitly supplies no ORM source. A nested
        schema has its own entry, and leaving its validation restores the enclosing source.
        """
        try:
            return instance.__orm_obj__  # type: ignore[attr-defined]
        except AttributeError:
            current = PydanticModel._orm_validation_source.get()
            if current is not None and current.schema is type(instance):
                return current.obj
        return None

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        """Construct from trusted field values without borrowing an enclosing validation source."""
        token = PydanticModel._orm_validation_source.set(None)
        try:
            instance = super().model_construct(_fields_set=_fields_set, **values)
            object.__setattr__(instance, "__orm_obj__", None)
            return instance
        finally:
            PydanticModel._orm_validation_source.reset(token)

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
