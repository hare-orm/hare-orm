from __future__ import annotations

import contextvars
import types
from typing import TYPE_CHECKING, Any, ClassVar, Union, cast, get_args, get_origin, get_type_hints

import pydantic
from pydantic import BaseModel, ConfigDict

from hare.contrib.pydantic.constants import MODEL_INDEX_MAX_SIZE
from hare.contrib.pydantic.enums import FetchedFieldCheck
from hare.core.cache import Cache
from hare.exceptions import NoValuesFetched
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.query.queryset import QuerySet
from hare.query.queryset.none_result import NoneAwaitable
from hare.query.relation_loading.prefetch_related_objects import prefetch_related_objects

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.models import Model
    from hare.query.queryset import QuerySetSingle


class PydanticModel(BaseModel):
    """Pydantic BaseModel for Hare objects.

    Provides extra methods on top of Pydantic's own model properties
    (https://docs.pydantic.dev/latest/usage/models/#model-properties).
    """

    model_config = ConfigDict(from_attributes=True)

    #: (schema class,) -> what each schema field is checked for, and whether the schema's own fetch
    #: fields prefetch it (``_get_fetched_field_checks()``).
    fetched_field_checks: ClassVar[Cache[tuple[tuple[str, FetchedFieldCheck, bool], ...]]] = Cache(
        MODEL_INDEX_MAX_SIZE
    )
    #: (schema class,) -> the relations it prefetches (``_get_fetch_fields()``).
    fetch_fields: ClassVar[Cache[list[str]]] = Cache(MODEL_INDEX_MAX_SIZE)
    #: Set while a schema validates instances whose relations its own fetch fields just prefetched -
    #: those aren't checked again on every instance.
    validating_prefetched: ClassVar[contextvars.ContextVar[bool]] = contextvars.ContextVar(
        "hare_pydantic_validating_prefetched", default=False
    )

    @pydantic.model_validator(mode="wrap")
    @classmethod
    def _hare_wrap(cls, values, handler):
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
        skips_prefetched = cls.validating_prefetched.get()
        # Only an instance loaded with .only()/.defer() can lack a column.
        is_partial = obj._partial
        for field_name, check, prefetched in cls._get_fetched_field_checks():
            if check is FetchedFieldCheck.COLUMN:
                if is_partial and not hasattr(obj, field_name):
                    raise NoValuesFetched(
                        f"Field '{field_name}' has not been fetched - the instance was loaded with "
                        "a .only()/.defer() that excluded it. Fetch it (or drop the field from this "
                        "schema) before serializing."
                    )
            elif prefetched and skips_prefetched:
                continue
            elif check is FetchedFieldCheck.RELATED_INSTANCE:
                value = getattr(obj, field_name)
                if value is NoneAwaitable or isinstance(value, QuerySet):
                    raise NoValuesFetched(
                        f"Field '{field_name}' has not been fetched - call prefetch_related_objects"
                        f"([obj], '{field_name}') or use select_related()/prefetch_related() before "
                        "serializing."
                    )
            elif not obj._get_relation(field_name)._fetched:
                raise NoValuesFetched(
                    f"Field '{field_name}' has not been fetched - call prefetch_related_objects"
                    f"([obj], '{field_name}') or use prefetch_related() before serializing."
                )

    @classmethod
    def _get_fetched_field_checks(cls) -> tuple[tuple[str, FetchedFieldCheck, bool], ...]:
        """What each field of the schema is checked for on an instance, and whether the schema's
        own fetch fields prefetch it - worked out once per schema class.

        Returns:
            The field name, its check and whether it is prefetched, per checked field.
        """
        key = (cls,)
        checks = PydanticModel.fetched_field_checks.get(key)
        if checks is None:
            model_class: type[Model] = cast("Any", cls.model_config)["orig_model"]
            prefetched_names = {fetch_field.split("__", 1)[0] for fetch_field in cls._get_fetch_fields()}
            found_checks = []
            for field_name in cls.model_fields:
                field = model_class._meta.fields_map.get(field_name)
                if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance, BackwardOneToOneRelation)):
                    found_checks.append(
                        (field_name, FetchedFieldCheck.RELATED_INSTANCE, field_name in prefetched_names)
                    )
                elif isinstance(field, (BackwardFKRelation, ManyToManyFieldInstance)):
                    found_checks.append((field_name, FetchedFieldCheck.RELATED_ROWS, field_name in prefetched_names))
                elif field is not None:
                    found_checks.append((field_name, FetchedFieldCheck.COLUMN, False))
            checks = PydanticModel.fetched_field_checks[key] = tuple(found_checks)
        return checks

    @classmethod
    def _validate_prefetched(cls, objects: list[Any]) -> list[Self]:
        """Validates instances whose relations the schema's own fetch fields just prefetched -
        without checking those relations again on every instance.

        Args:
            objects: The instances.

        Returns:
            The schema instances.
        """
        token = cls.validating_prefetched.set(True)
        try:
            return [cls.model_validate(obj) for obj in objects]
        finally:
            cls.validating_prefetched.reset(token)

    @classmethod
    def _get_fetch_fields(cls) -> list[str]:
        """Recursively collects fields needed to fetch, based on cls's own annotations and the
        Hare model recorded on cls.model_config["orig_model"].

        Returns:
            The list of fields to be fetched.
        """
        key = (cls,)
        known_fetch_fields = PydanticModel.fetch_fields.get(key)
        if known_fetch_fields is not None:
            return list(known_fetch_fields)
        model_class: type[Model] = cast("Any", cls.model_config)["orig_model"]
        fetch_fields = []
        # get_type_hints() - __annotations__ holds only the class's own annotations, without its
        # bases'.
        for field_name, field_type in get_type_hints(cls).items():
            field_type = cast("Any", field_type)
            origin = cast("Any", get_origin(field_type))
            if origin is list:
                args = get_args(field_type)
                if args:
                    field_type = args[0]
            elif origin is Union or origin is types.UnionType:
                args = get_args(field_type)
                for arg in args:
                    if arg is not type(None):
                        field_type = arg
                        break

            if not isinstance(field_type, type):
                continue
            if field_name in model_class._meta.fetch_fields and issubclass(field_type, PydanticModel):
                subclass_fetch_fields = field_type._get_fetch_fields()
                if subclass_fetch_fields:
                    fetch_fields.extend([field_name + "__" + f for f in subclass_fetch_fields])
                else:
                    fetch_fields.append(field_name)

        PydanticModel.fetch_fields[key] = fetch_fields
        return list(fetch_fields)

    @classmethod
    async def from_hare_orm(cls, obj: Model) -> Self:
        """Builds the pydantic model from ``obj``, fetching its relations first. ``from_orm()`` is the
        synchronous variant - fetch or exclude the relations yourself.

        Args:
            obj: The model instance.
        """
        fetch_fields = cls._get_fetch_fields()
        await prefetch_related_objects([obj], *fetch_fields)
        return cls._validate_prefetched([obj])[0]

    @classmethod
    async def from_queryset_single(cls, queryset: QuerySetSingle[Model]) -> Self:
        """Returns a serializable pydantic model instance for a single model from the provided
        queryset.

        This will prefetch all the relations automatically.

        Args:
            queryset: a queryset on the model this PydanticModel is based on.
        """
        fetch_fields = cls._get_fetch_fields()
        return cls._validate_prefetched([await queryset.prefetch_related(*fetch_fields)])[0]

    @classmethod
    async def from_queryset(cls, queryset: QuerySet[Model]) -> list[Self]:
        """Returns a serializable pydantic model instance that contains a list of models, from
        the provided queryset.

        This will prefetch all the relations automatically.

        Args:
            queryset: a queryset on the model this PydanticModel is based on.
        """
        fetch_fields = cls._get_fetch_fields()
        return cls._validate_prefetched(list(await queryset.prefetch_related(*fetch_fields)))
